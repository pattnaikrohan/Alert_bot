"""Every AI call, metered: real token counts as the provider reported them, which part of the app asked, how long it
took, and what it cost. And what the same work would have cost on other providers' models, at their live prices.

    record()      called by the AI client for every request it makes (answered, empty, failed or retried)
    tagged()      callers say which project / person a call is for (the feature is found from who called)
    catalog()     live prices for every model OpenRouter sells (chat and embeddings), refreshed twice a day
    summary()     the AI usage page: totals, by feature / model / day / project, recent calls, scenario costs

Only counts are kept: never a prompt, an answer, or anything the model was shown.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import logging
import queue
import re
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..timeutil import iso, utcnow

log = logging.getLogger(__name__)

# ── which part of the app asked ───────────────────────────────────────────────

FEATURES: dict[str, dict[str, str]] = {
    "incident_analysis": {"label": "Incident analysis", "hint": "The write-up of each new or worse incident: what happened, why, impact, what to do"},
    "triage": {"label": "Incident triage", "hint": "Should this escalate, and how urgent is it (calibrated probabilities)"},
    "similar": {"label": "Similar past incidents", "hint": "Embeddings that find incidents like this one"},
    "ask": {"label": "Ask the bot", "hint": "Questions in plain language, answered from the bot's own facts"},
    "charts": {"label": "Chart requests", "hint": "Turning \"show me … as a pie chart\" into a chart"},
    "dq": {"label": "Data-quality explanations", "hint": "The analyst's note on each table after its data checks (aggregates only)"},
    "report": {"label": "Morning report", "hint": "The daily summary for Teams"},
    "voice": {"label": "Robot voice", "hint": "Reading answers aloud"},
    "fixes": {"label": "Fix steps", "hint": "Step-by-step fixes for errors the library doesn't know, and new approaches when one didn't work"},
    "rules": {"label": "Data rules bot", "hint": "Rules about what each column's values should look like, from table and column names only"},
    "other": {"label": "Other", "hint": "Anything else that called a model"},
}
_BY_MODULE = {"alertbot.ai.investigator": "incident_analysis", "alertbot.ai.ask": "ask", "alertbot.ai.charts": "charts",
              "alertbot.dq.graph": "dq", "alertbot.engine.report": "report", "alertbot.api.main": "voice"}
_KIND_FEATURE = {"decide": "triage", "embed": "similar", "speech": "voice"}

_TAG: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("ai_usage_tag", default=None)


@contextlib.contextmanager
def tagged(**tag: Any) -> Iterator[None]:
    """Calls made inside are for this project / person / feature (project=key, user_id=id, feature=name)."""
    token = _TAG.set({**(_TAG.get() or {}), **{k: v for k, v in tag.items() if v is not None}})
    try:
        yield
    finally:
        _TAG.reset(token)


def _caller_feature() -> str:
    f = sys._getframe(2)
    while f is not None:
        mod = f.f_globals.get("__name__", "")
        if mod not in ("alertbot.ai.llm", __name__) and not mod.startswith(("contextlib", "httpx")):
            for prefix, feature in _BY_MODULE.items():
                if mod == prefix:
                    return feature
            if mod.startswith("alertbot."):
                return "other"
        f = f.f_back
    return "other"


# ── recording (never blocks the call, never breaks it) ─────────────────────────

_Q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=20000)
_WRITER: threading.Thread | None = None
_LOCK = threading.Lock()


def estimate_tokens(chars: int) -> int:
    """About four characters per token for English and JSON (used only when a provider doesn't report usage)."""
    return max(0, round(chars / 4))


def record(*, kind: str, model: str, status: str, served_model: str | None = None, usage: dict[str, Any] | None = None,
           latency_ms: float = 0, http_status: int | None = None, error: str | None = None, request_chars: int = 0,
           response_chars: int = 0, characters: int = 0, audio_seconds: float | None = None) -> None:
    try:
        tag = _TAG.get() or {}
        u = usage or {}
        prompt, completion = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        estimated = False
        if not u and status in ("ok", "empty", "retry") and (request_chars or response_chars):
            prompt, completion, estimated = estimate_tokens(request_chars), estimate_tokens(response_chars), True
        row = {
            "at": utcnow(), "feature": tag.get("feature") or _KIND_FEATURE.get(kind) or _caller_feature(), "kind": kind,
            "model": model[:200], "served_model": (served_model or None) and served_model[:200], "status": status,
            "http_status": http_status, "prompt_tokens": prompt, "completion_tokens": completion,
            "reasoning_tokens": int(((u.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0),
            "cached_tokens": int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0),
            "estimated": estimated, "cost_usd": float(u["cost"]) if u.get("cost") is not None else None,
            "latency_ms": int(latency_ms), "characters": characters, "audio_seconds": audio_seconds,
            "project_key": tag.get("project"), "user_id": tag.get("user_id"), "error": (error or None) and error[:200],
        }
        _Q.put_nowait(row)
        _ensure_writer()
    except Exception:  # noqa: BLE001 - metering must never get in the way of an answer
        log.debug("couldn't meter an AI call", exc_info=True)


def _ensure_writer() -> None:
    global _WRITER
    with _LOCK:
        if _WRITER is None or not _WRITER.is_alive():
            _WRITER = threading.Thread(target=_write_loop, name="ai-usage-writer", daemon=True)
            _WRITER.start()


def _drain() -> list[dict[str, Any]]:
    rows = []
    while True:
        try:
            rows.append(_Q.get_nowait())
        except queue.Empty:
            return rows


def flush() -> int:
    """Write what's waiting now (in its own short transaction). Returns how many calls were written."""
    rows = _drain()
    if not rows:
        return 0
    from ..db import AiCall, session_scope

    try:
        with session_scope() as s:
            s.add_all(AiCall(**r) for r in rows)
        return len(rows)
    except Exception:  # noqa: BLE001 - e.g. the database is busy: put them back, try again shortly
        for r in rows:
            with contextlib.suppress(queue.Full):
                _Q.put_nowait(r)
        log.debug("AI usage write deferred", exc_info=True)
        return 0


def _write_loop() -> None:
    while True:
        time.sleep(2)
        flush()


atexit.register(flush)


def mp3_seconds(data: bytes) -> float | None:
    """Length of an mp3 from its first frame's bitrate (constant-bitrate speech audio), or None."""
    for i in range(min(len(data) - 4, 64 * 1024)):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            version, layer, idx = (data[i + 1] >> 3) & 3, (data[i + 1] >> 1) & 3, data[i + 2] >> 4
            if layer != 1 or idx in (0, 15):  # layer III only
                continue
            table = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320] if version == 3 else \
                [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
            return round(len(data) * 8 / (table[idx - 1] * 1000), 2)
    return None


# ── live prices (OpenRouter's public catalogue: no key, nothing sent) ─────────

CATALOG_KEY = "ai-prices"
CATALOG_EVERY = timedelta(hours=12)
ROUTER = "https://openrouter.ai/api/v1"
_SKIP = re.compile(r"guard|embed|vision|image|banana|audio|tts|realtime|search|codestral|devstral|coder|multi-agent|build|-exp|preview|omni|"
                   r"deep-research|safeguard|moderation|ocr", re.I)
PROVIDERS = {"openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "x-ai": "xAI", "deepseek": "DeepSeek",
             "mistralai": "Mistral", "meta-llama": "Meta", "qwen": "Qwen", "voyageai": "Voyage AI", "cohere": "Cohere",
             "amazon": "Amazon", "microsoft": "Microsoft", "nvidia": "NVIDIA"}


def _per_m(v: Any) -> float:
    try:
        return round(float(v) * 1_000_000, 6)
    except (TypeError, ValueError):
        return 0.0


def _fetch() -> dict[str, Any]:
    with httpx.Client(timeout=20) as h:
        chat = h.get(f"{ROUTER}/models").json()["data"]
        try:
            emb = h.get(f"{ROUTER}/embeddings/models").json()["data"]
        except Exception:  # noqa: BLE001 - embeddings prices are a bonus
            emb = []
    models = {}
    for m, kind in [(x, "chat") for x in chat] + [(x, "embed") for x in emb]:
        p = m.get("pricing") or {}
        outs = (m.get("architecture") or {}).get("output_modalities") or ["text"]
        if kind == "chat" and "text" not in outs:
            continue
        provider = m["id"].split("/", 1)[0]
        models[m["id"]] = {"id": m["id"], "name": m.get("name") or m["id"], "provider": provider,
                           "provider_name": PROVIDERS.get(provider, provider.replace("-", " ").title()),
                           "kind": kind, "input": _per_m(p.get("prompt")), "output": _per_m(p.get("completion")),
                           "cached_input": _per_m(p.get("input_cache_read")) or None, "created": m.get("created") or 0,
                           "context": m.get("context_length")}
    return {"fetched_at": iso(utcnow()), "source": f"{ROUTER}/models", "models": models}


def catalog(session: Session | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """Prices per million tokens for every model OpenRouter sells. Fetched at most twice a day (the network call is made
    with no database write held), kept, and the last good copy is used if OpenRouter can't be reached."""
    from ..db import BotState, session_scope

    with session_scope() as s:
        row = s.get(BotState, CATALOG_KEY)
        cached = dict(row.value) if row and row.value else None
        fresh = row is not None and utcnow() - row.updated_at < CATALOG_EVERY
    if cached and fresh and not refresh:
        return cached
    try:
        got = _fetch()
    except Exception as exc:  # noqa: BLE001
        log.info("AI price list unavailable: %s", exc)
        return cached or {"fetched_at": None, "source": None, "models": {}, "error": "Couldn't reach OpenRouter's price list."}
    with session_scope() as s:
        row = s.get(BotState, CATALOG_KEY) or BotState(key=CATALOG_KEY)
        row.value, row.updated_at = got, utcnow()
        s.add(row)
    return got


def _text_models(models: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    return sorted((m for m in models.values() if m["kind"] == "chat" and m["provider"] == provider and ":" not in m["id"]
                   and m["input"] > 0 and not _SKIP.search(m["id"])), key=lambda m: -m["created"])


def default_scenarios(models: dict[str, Any]) -> list[str]:
    """What to compare against when no one has chosen: each big provider's newest top model and newest efficient one
    (for Anthropic: Opus, Sonnet and Haiku), from the live list, so it stays current by itself."""
    out: list[str] = []
    for family in ("opus", "sonnet", "haiku"):
        hit = next((m for m in _text_models(models, "anthropic") if f"claude-{family}" in m["id"]), None)
        if hit:
            out.append(hit["id"])
    for provider, n in (("openai", 2), ("google", 2), ("x-ai", 1), ("deepseek", 1), ("mistralai", 1), ("meta-llama", 1)):
        recent = _text_models(models, provider)[:8]
        if not recent:
            continue
        top = max(recent, key=lambda m: (m["input"] + m["output"], m["created"]))
        picks = [top] + ([min(recent, key=lambda m: (m["input"] + m["output"], -m["created"]))] if n > 1 else [])
        out += [m["id"] for m in picks if m["id"] not in out]
    return out


def embedding_for(models: dict[str, Any], provider: str) -> dict[str, Any] | None:
    """The embedding model a team on this provider would use (Anthropic has none: it recommends Voyage AI)."""
    emb = sorted((m for m in models.values() if m["kind"] == "embed" and m["input"] > 0 and ":" not in m["id"]
                  and not re.search(r"code|multimodal|preview|lite|ada-002", m["id"])), key=lambda m: -m["created"])
    for want in ([provider] if provider != "anthropic" else []) + (["voyageai"] if provider == "anthropic" else []) + ["openai"]:
        hit = next((m for m in emb if m["provider"] == want), None)
        if hit:
            return hit
    return emb[0] if emb else None


# ── the page ──────────────────────────────────────────────────────────────────


def _price(tokens_in: int, tokens_out: int, emb_tokens: int, m: dict[str, Any], e: dict[str, Any] | None) -> dict[str, float]:
    i, o = tokens_in * m["input"] / 1e6, tokens_out * m["output"] / 1e6
    x = emb_tokens * (e["input"] if e else 0) / 1e6
    return {"input": round(i, 6), "output": round(o, 6), "embeddings": round(x, 6), "total": round(i + o + x, 6)}


def summary(session: Session, *, days: int, project_keys: list[str] | None, user_id: int | None, scenarios: list[str] | None) -> dict[str, Any]:
    from ..db import AiCall

    now = utcnow()
    since = now - timedelta(days=days)
    q = select(AiCall).where(AiCall.at >= since)
    if project_keys is not None:  # not an admin: their projects, their own calls, and calls for no project in particular
        q = q.where((AiCall.project_key.in_(project_keys or ["-"])) | (AiCall.user_id == user_id) | (AiCall.project_key.is_(None)))
    calls = list(session.scalars(q.order_by(AiCall.at)))

    cat = catalog(session)
    models = cat.get("models") or {}
    chosen = [m for m in (scenarios or default_scenarios(models)) if m in models]

    tok = lambda c: c.prompt_tokens + c.completion_tokens  # noqa: E731
    gen = [c for c in calls if c.kind in ("chat", "decide")]
    emb = [c for c in calls if c.kind == "embed"]
    voice = [c for c in calls if c.kind == "speech"]
    t_in, t_out = sum(c.prompt_tokens for c in gen), sum(c.completion_tokens for c in gen)
    e_in = sum(c.prompt_tokens for c in emb)
    answered = [c for c in calls if c.status in ("ok",)]
    first = calls[0].at if calls else None
    span_days = max(1.0, ((now - first).total_seconds() / 86400) if first else 1.0)

    def bucket(rows: list[Any]) -> dict[str, Any]:
        g = [c for c in rows if c.kind in ("chat", "decide")]
        e = [c for c in rows if c.kind == "embed"]
        ok = [c for c in rows if c.status == "ok"]
        return {"calls": len(rows), "answered": len(ok), "failed": sum(c.status == "error" for c in rows),
                "prompt_tokens": sum(c.prompt_tokens for c in g), "completion_tokens": sum(c.completion_tokens for c in g),
                "embedding_tokens": sum(c.prompt_tokens for c in e), "reasoning_tokens": sum(c.reasoning_tokens for c in rows),
                "tokens": sum(tok(c) for c in rows), "estimated_calls": sum(c.estimated for c in rows),
                "actual_cost": round(sum(c.cost_usd or 0 for c in rows), 6),
                "avg_latency_ms": round(sum(c.latency_ms for c in ok) / len(ok)) if ok else None,
                "audio_seconds": round(sum(c.audio_seconds or 0 for c in rows), 1), "characters": sum(c.characters for c in rows),
                "cost": {mid: _price(sum(c.prompt_tokens for c in g), sum(c.completion_tokens for c in g),
                                     sum(c.prompt_tokens for c in e), models[mid], embedding_for(models, models[mid]["provider"]))["total"]
                         for mid in chosen}}

    by_feature = defaultdict(list)
    by_model = defaultdict(list)
    by_project = defaultdict(list)
    by_day: dict[str, list[Any]] = {}
    for d in range(days):
        by_day[(since + timedelta(days=d + 1)).date().isoformat()] = []
    for c in calls:
        by_feature[c.feature].append(c)
        by_model[c.served_model or c.model].append(c)
        by_project[c.project_key or ""].append(c)
        by_day.setdefault(c.at.date().isoformat(), []).append(c)

    scen = []
    for mid in chosen:
        m = models[mid]
        e = embedding_for(models, m["provider"])
        p = _price(t_in, t_out, e_in, m, e)
        scen.append({**{k: m[k] for k in ("id", "name", "provider", "provider_name", "input", "output", "cached_input", "context")},
                     "embedding_model": e and {"id": e["id"], "name": e["name"], "input": e["input"]},
                     "batch": next(({"input": b["input"], "output": b["output"]} for b in [models.get(f"{mid}:batch")] if b), None),
                     "cost": p, "per_month": round(p["total"] / span_days * 30, 4),
                     "per_1k_calls": round(p["total"] / len(answered) * 1000, 4) if answered else None})
    scen.sort(key=lambda s: s["cost"]["total"])

    served = sorted(by_model.items(), key=lambda kv: -len(kv[1]))
    return {
        "days": days, "generated_at": iso(now), "first_call_at": iso(first), "span_days": round(span_days, 2),
        "totals": {**bucket(calls), "tokens_per_day": round(sum(tok(c) for c in calls) / span_days),
                   "generation": {"prompt_tokens": t_in, "completion_tokens": t_out}, "embedding_tokens": e_in,
                   "voice": {"calls": len(voice), "characters": sum(c.characters for c in voice),
                             "audio_seconds": round(sum(c.audio_seconds or 0 for c in voice), 1)}},
        "features": sorted(({"key": k, **FEATURES.get(k, FEATURES["other"]), **bucket(v)} for k, v in by_feature.items()),
                           key=lambda f: -f["tokens"]),
        "models": [{"model": k, "price": next(({"input": models[i]["input"], "output": models[i]["output"]}
                                               for i in (k, k.split("@")[0]) if i in models), None),
                    "free": k.endswith(":free") or (k in models and models[k]["input"] == 0 and models[k]["output"] == 0),
                    **{x: y for x, y in bucket(v).items() if x != "cost"}} for k, v in served],
        "projects": [{"project": k or None, **{x: y for x, y in bucket(v).items() if x != "cost"}} for k, v in by_project.items()],
        "daily": [{"date": d, "prompt_tokens": sum(c.prompt_tokens for c in v if c.kind != "embed"),
                   "completion_tokens": sum(c.completion_tokens for c in v), "embedding_tokens": sum(c.prompt_tokens for c in v if c.kind == "embed"),
                   "calls": len(v)} for d, v in sorted(by_day.items())][-days:],
        "recent": [{"at": iso(c.at), "feature": c.feature, "kind": c.kind, "model": c.model, "served_model": c.served_model,
                    "status": c.status, "http_status": c.http_status, "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens, "reasoning_tokens": c.reasoning_tokens, "estimated": c.estimated,
                    "latency_ms": c.latency_ms, "project": c.project_key, "cost_usd": c.cost_usd,
                    "audio_seconds": c.audio_seconds, "error": c.error} for c in reversed(calls[-60:])],
        "scenarios": scen,
        "prices": {"fetched_at": cat.get("fetched_at"), "source": cat.get("source"), "error": cat.get("error"),
                   "count": len(models)},
        "features_catalog": FEATURES,
    }


def price_list(session: Session, q: str = "", limit: int = 60) -> list[dict[str, Any]]:
    """Models to compare against, for the picker: chat models with a price, newest first, matching the search."""
    models = (catalog(session).get("models") or {}).values()
    words = [w for w in q.lower().split() if w]
    hits = [m for m in models if m["kind"] == "chat" and m["input"] > 0 and ":" not in m["id"]
            and all(w in f"{m['id']} {m['name']} {m['provider_name']}".lower() for w in words)]
    hits.sort(key=lambda m: -m["created"])
    return [{k: m[k] for k in ("id", "name", "provider", "provider_name", "input", "output", "context")} for m in hits[:limit]]


def since_days(days: int) -> datetime:
    return utcnow() - timedelta(days=days)
