"""The bot's models, through OpenRouter, each with a fallback.

    chat        explanations, investigation write-ups, answers, the morning summary
                → a chain of models; a model that is rate-limited, down, slow or refuses is skipped (and rested for a
                  while), the next one is tried, and if every model fails the caller keeps its rule-based text
    decide      triage: escalate? how urgent? (calibrated probabilities)      → fallback: the rules' severity
    embed       similar past incidents                                         → fallback: same-table history
    speak       the robot's voice (mp3)                                        → fallback: the browser's own voice

Nothing here can break detection or alerting: every call has a timeout and every caller has a non-AI path.
The API key is a secret reference (env:// or kv://), never stored in config.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx

from ..config import AIConfig, load_ai_config
from ..secrets import resolve_secret
from . import usage

log = logging.getLogger(__name__)

BASE = "https://openrouter.ai/api/v1"


class AIUnavailable(RuntimeError):
    """No model in the chain could answer (or AI is switched off)."""


@dataclass
class ModelHealth:
    """Per-model circuit breaker: a failing model is rested so we don't wait on it every call."""

    resting_until: dict[str, float] = field(default_factory=dict)
    last_error: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def ok(self, model: str) -> bool:
        with self.lock:
            return time.time() >= self.resting_until.get(model, 0)

    def rest(self, model: str, seconds: float, why: str) -> None:
        with self.lock:
            self.resting_until[model] = time.time() + seconds
            self.last_error[model] = why[:300]
        log.info("AI model %s resting %.0fs: %s", model, seconds, why[:160])

    def status(self) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            return {m: {"resting_for_s": max(0, round(t - now)), "last_error": self.last_error.get(m)}
                    for m, t in self.resting_until.items()}


HEALTH = ModelHealth()


@dataclass
class ChatResult:
    text: str
    model: str
    tried: list[str]


def _rest_for(status: int, body: str) -> float:
    if status in (401,):
        return 3600  # bad key: nothing will work until it's fixed
    if status in (402, 403, 404):
        return 6 * 3600  # model not available to this key/app (e.g. "only on agentic harnesses")
    if status == 429:
        if "per-day" in body or "per day" in body:  # the account's daily free allowance: wait for its reset
            m = re.search(r'X-RateLimit-Reset"?\s*:\s*"?(\d{10,13})', body)
            now = time.time()
            if m:
                reset = int(m.group(1)) / (1000 if len(m.group(1)) > 10 else 1)
            else:
                reset = (now // 86400 + 1) * 86400  # next midnight UTC
            return min(24 * 3600, max(90, reset - now + 30))
        return 90  # per-minute rate limit
    return 120  # 5xx, timeouts, empty answers


class OpenRouter:
    def __init__(self, ai: AIConfig, transport: httpx.BaseTransport | None = None):
        self.ai = ai
        key = resolve_secret(ai.api_key)
        if not key:
            raise AIUnavailable("no OpenRouter API key configured")
        self._http = httpx.Client(base_url=BASE, transport=transport, timeout=ai.timeout_seconds, headers={
            "Authorization": f"Bearer {key}", "HTTP-Referer": "https://alertbot.local", "X-Title": "Alert Bot"})

    # ── chat, with a fallback chain ──
    def chat(self, messages: list[dict[str, str]], *, max_tokens: int = 3000, json_mode: bool = False,
             temperature: float = 0.2) -> ChatResult:
        from ..dq.privacy import scrub_messages

        messages = scrub_messages(messages)  # the AI boundary: values a message quotes never reach a model
        tried: list[str] = []
        chain = [m for m in self.ai.chat_models if HEALTH.ok(m)] or list(self.ai.chat_models)  # all resting: try anyway
        sent = sum(len(m.get("content") or "") for m in messages)
        for model in chain:
            tried.append(model)
            # these are reasoning models; the rules already did the investigating, so the model only writes it up.
            # With reasoning on, long evidence made them spend the whole budget thinking and return nothing.
            body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                                    "reasoning": {"enabled": False}, "usage": {"include": True}}  # real token counts and cost
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            t0 = time.perf_counter()
            try:
                r = self._http.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:
                usage.record(kind="chat", model=model, status="error", latency_ms=(time.perf_counter() - t0) * 1000, error=type(exc).__name__)
                HEALTH.rest(model, 120, f"{type(exc).__name__}: {exc}")
                continue
            if r.status_code == 400 and json_mode:  # some models reject response_format: retry once without it
                usage.record(kind="chat", model=model, status="error", http_status=400, latency_ms=(time.perf_counter() - t0) * 1000,
                             error="rejected JSON mode; retried without it")
                body.pop("response_format", None)
                t0 = time.perf_counter()
                r = self._http.post("/chat/completions", json=body)
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code != 200:
                usage.record(kind="chat", model=model, status="error", http_status=r.status_code, latency_ms=ms, error=f"HTTP {r.status_code}")
                HEALTH.rest(model, _rest_for(r.status_code, r.text), f"HTTP {r.status_code}: {r.text[:200]}")
                continue
            data = r.json()
            if data.get("error"):
                usage.record(kind="chat", model=model, status="error", http_status=200, latency_ms=ms, usage=data.get("usage"),
                             error=str((data["error"] or {}).get("code") or "provider error") if isinstance(data["error"], dict) else "provider error")
                HEALTH.rest(model, 120, json.dumps(data["error"])[:200])
                continue
            choice = (data.get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content") or ""
            usage.record(kind="chat", model=model, served_model=data.get("model"), usage=data.get("usage"), latency_ms=ms, http_status=200,
                         status="ok" if text.strip() else "empty", request_chars=sent, response_chars=len(text))
            if not text.strip() and choice.get("finish_reason") == "length":
                # ran out of room before answering: one retry on the same model with double the budget
                body["max_tokens"] = max_tokens * 2
                t0 = time.perf_counter()
                r2 = self._http.post("/chat/completions", json=body)
                if r2.status_code == 200:
                    d2 = r2.json()
                    text = (((d2.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
                    usage.record(kind="chat", model=model, served_model=d2.get("model"), usage=d2.get("usage"), http_status=200,
                                 latency_ms=(time.perf_counter() - t0) * 1000, status="retry" if text.strip() else "empty",
                                 request_chars=sent, response_chars=len(text))
                else:
                    usage.record(kind="chat", model=model, status="error", http_status=r2.status_code,
                                 latency_ms=(time.perf_counter() - t0) * 1000, error=f"HTTP {r2.status_code}")
            if not text.strip():
                HEALTH.rest(model, 60, f"empty answer (finish: {choice.get('finish_reason')})")
                continue
            return ChatResult(text=text.strip(), model=data.get("model", model), tried=tried)
        raise AIUnavailable(f"no chat model answered (tried {', '.join(tried) or 'none'})")

    def chat_json(self, system: str, user: str, *, max_tokens: int = 3000) -> tuple[dict[str, Any], ChatResult]:
        res = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                        max_tokens=max_tokens, json_mode=True)
        return parse_json(res.text), res

    # ── triage decisions (calibrated probabilities) ──
    def decide(self, questions: dict[str, Any], state: dict[str, Any] | str) -> dict[str, Any]:
        model = self.ai.decision_model
        if not model or not HEALTH.ok(model):
            raise AIUnavailable("decision model unavailable")
        payload = {"model": model, "questions": questions, "state": state}
        t0 = time.perf_counter()
        try:
            r = self._http.post("/systemone", json=payload)
        except httpx.HTTPError as exc:
            usage.record(kind="decide", model=model, status="error", latency_ms=(time.perf_counter() - t0) * 1000, error=type(exc).__name__)
            HEALTH.rest(model, 120, str(exc))
            raise AIUnavailable(str(exc)) from exc
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            usage.record(kind="decide", model=model, status="error", http_status=r.status_code, latency_ms=ms, error=f"HTTP {r.status_code}")
            HEALTH.rest(model, _rest_for(r.status_code, r.text), f"HTTP {r.status_code}: {r.text[:200]}")
            raise AIUnavailable(f"decision model HTTP {r.status_code}")
        data = r.json()
        answers = data.get("answers") or {}
        usage.record(kind="decide", model=model, served_model=data.get("model"), usage=data.get("usage"), status="ok", http_status=200,
                     latency_ms=ms, request_chars=len(json.dumps(payload, default=str)), response_chars=len(json.dumps(answers, default=str)))
        return answers

    # ── embeddings ──
    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self.ai.embedding_model
        if not model or not HEALTH.ok(model):
            raise AIUnavailable("embedding model unavailable")
        try:
            from ..dq.privacy import scrub_for_llm

            inputs = [scrub_for_llm(t[:1500]) for t in texts]
            t0 = time.perf_counter()
            r = self._http.post("/embeddings", json={"model": model, "input": inputs})
        except httpx.HTTPError as exc:
            usage.record(kind="embed", model=model, status="error", error=type(exc).__name__)
            HEALTH.rest(model, 120, str(exc))
            raise AIUnavailable(str(exc)) from exc
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            usage.record(kind="embed", model=model, status="error", http_status=r.status_code, latency_ms=ms, error=f"HTTP {r.status_code}")
            HEALTH.rest(model, _rest_for(r.status_code, r.text), f"HTTP {r.status_code}: {r.text[:200]}")
            raise AIUnavailable(f"embedding model HTTP {r.status_code}")
        data = r.json()
        usage.record(kind="embed", model=model, served_model=data.get("model"), usage=data.get("usage"), status="ok", http_status=200,
                     latency_ms=ms, request_chars=sum(len(t) for t in inputs))
        return [d["embedding"] for d in sorted(data["data"], key=lambda d: d.get("index", 0))]

    # ── speech ──
    def speak(self, text: str) -> bytes:
        model = self.ai.speech_model
        if not model or not HEALTH.ok(model):
            raise AIUnavailable("speech model unavailable")
        try:
            from ..dq.privacy import scrub_for_llm

            said = scrub_for_llm(text[:1500])
            t0 = time.perf_counter()
            r = self._http.post("/audio/speech", json={"model": model, "input": said, "response_format": "mp3"})
        except httpx.HTTPError as exc:
            usage.record(kind="speech", model=model, status="error", error=type(exc).__name__)
            HEALTH.rest(model, 120, str(exc))
            raise AIUnavailable(str(exc)) from exc
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200 or not r.content:
            usage.record(kind="speech", model=model, status="error", http_status=r.status_code, latency_ms=ms, error=f"HTTP {r.status_code}")
            HEALTH.rest(model, _rest_for(r.status_code, r.text if r.status_code != 200 else ""), f"HTTP {r.status_code}")
            raise AIUnavailable(f"speech model HTTP {r.status_code}")
        usage.record(kind="speech", model=model, status="ok", http_status=200, latency_ms=ms, characters=len(said),
                     audio_seconds=usage.mp3_seconds(r.content))
        return r.content


@lru_cache(maxsize=1)
def _client_for(key: str) -> OpenRouter:  # cached per config snapshot
    return OpenRouter(load_ai_config())


def client() -> OpenRouter:
    ai = load_ai_config()
    if not ai.ready:
        raise AIUnavailable("AI is switched off in ai.yaml")
    return _client_for(f"{ai.provider}|{','.join(ai.chat_models)}|{ai.api_key}")


def parse_json(text: str) -> dict[str, Any]:
    """Models sometimes wrap JSON in prose or code fences; take the outermost object."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the model's answer")
    return json.loads(t[start:end + 1])


_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")


def numbers_in(text: str) -> set[str]:
    out = set()
    for n in _NUM.findall(text or ""):
        v = n.replace(",", "").rstrip("%")
        try:
            f = float(v)
        except ValueError:
            continue
        if f < 10 and "." not in v:  # list numbering, "3 tables", hours: too common to police
            continue
        out.add(f"{round(f, 1):g}")
    return out


def invented_numbers(answer: str, evidence: str) -> set[str]:
    """Numbers the model wrote that appear nowhere in what it was given: a sign it made something up."""
    have = numbers_in(evidence)
    # allow simple derived values of evidence numbers (rounded, thousands as "k", percentages from ratios aren't checked)
    loose = {f"{round(float(x)):g}" for x in have} | {f"{round(float(x) / 1000, 1):g}" for x in have}
    return {n for n in numbers_in(answer) if n not in have and n not in loose}
