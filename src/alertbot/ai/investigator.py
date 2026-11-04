"""The incident investigator.

1. Rules run first, always: they gather the evidence (run steps, folder contents, history, baselines) and write a
   complete explanation. This is the fallback for everything below and the only path when AI is off.
2. With AI on (OpenRouter):
   - a chat model rewrites the explanation the way an engineer would send it (what happened with expected / actual /
     deviation, the investigation trail, likely cause, impact, numbered actions), from the rules' evidence only;
   - a guard rejects the write-up if it states numbers that aren't in the evidence (made-up figures);
   - a decision model triages it (escalate today? how urgent?) with calibrated probabilities;
   - an embedding model finds similar past incidents in this project.
   Any AI failure keeps the rule-based analysis; detection and alerting never depend on a model.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from sqlalchemy import select

from ..config import load_ai_config
from ..db import Incident, IncidentEmbedding
from ..engine.checks import Outcome
from ..engine.context import ProjectContext
from .rules import Analysis, investigate_with_rules

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the AI investigator of a data reliability bot. Automated checks flagged a data incident and
a rule-based pass already wrote an exact technical report (it stays as it is, below your text).

Write the BRIEFING that goes on top of it: what a manager or on-call engineer needs in 2-4 short sentences.
Cover: what happened, why (most likely cause), what is affected, and the single most important next step.

Rules:
- Use ONLY facts from the evidence. Never invent numbers, names, causes, times or links.
- Use at most 3 numbers, each copied exactly from the evidence (e.g. "41,108 rows", "4 PROD tables").
- Put step, table and folder names in single quotes exactly as written in the evidence.
- No markdown, no bullet points, no links.

Return a JSON object: {"briefing": "...", "confidence": "low" | "medium" | "high"}"""


def investigate(ctx: ProjectContext, incident: Incident, failing: list[Outcome]) -> Analysis:
    """Inside the bot's cycle: the rule-based analysis only (fast). With AI on, the incident is queued for
    `enrich_with_ai`, which runs after the cycle has saved, so a slow model never holds the database."""
    rules = investigate_with_rules(ctx, incident, failing)
    if load_ai_config(ctx.settings.config_dir).ready:
        ctx.ai_queue.append(incident.id)
    return rules


def enrich_with_ai(cfg: Any, incident_id: int) -> bool:
    """AI write-up, triage and similar incidents for one incident: read (short), think (no DB), write (short)."""
    from ..db import CheckResult, IncidentEvent, Notification, session_scope
    from ..engine.cycle import make_context
    from ..engine.notify import build_payload
    from .llm import AIUnavailable, client

    try:
        llm = client()
    except AIUnavailable as exc:
        log.info("AI unavailable: %s", exc)
        return False
    # 1. read what the rules found
    with session_scope() as s:
        inc = s.get(Incident, incident_id)
        if inc is None or inc.status == "resolved" or inc.analysis_source == "ai":
            return False  # gone, or already enriched since the rules last wrote it
        latest: dict[str, CheckResult] = {}
        for r in s.scalars(select(CheckResult).where(CheckResult.incident_id == incident_id).order_by(CheckResult.created_at.desc())):
            latest.setdefault(r.check, r)
        failing = [Outcome(r.check, r.scope, "fail", r.severity, r.message, r.actual, r.expected_low, r.expected_high, r.run_ref,
                           inc.scope.split(":", 1)[-1], r.details) for r in latest.values()]
        rules = Analysis(summary=inc.summary, impact=inc.impact, likely_cause=inc.likely_cause, suggested_action=inc.suggested_action,
                         confidence=inc.confidence or "medium", evidence=[e for e in (inc.evidence or []) if e.get("tool") not in ("ai", "triage", "embeddings")])
        snapshot = {"id": inc.id, "title": inc.title, "scope": inc.scope, "severity": inc.severity, "project": cfg.display_name}
        s.expunge(inc)
    # 2. think (no database)
    result: Analysis = rules
    try:
        result = _write_up(snapshot["project"], inc, failing, rules, llm)
    except Exception as exc:  # noqa: BLE001 - keep the rule-based analysis
        log.warning("AI write-up failed for incident %s, keeping the rule-based analysis: %s", incident_id, exc)
    extra: list[dict[str, Any]] = []
    try:
        extra.append(_triage(inc, result, llm))
    except Exception as exc:  # noqa: BLE001
        log.info("AI triage skipped: %s", exc)
    vec: list[float] | None = None
    text = f"{snapshot['title']}. {result.likely_cause.splitlines()[0] if result.likely_cause else ''}"
    try:
        vec = llm.embed([text])[0]
    except Exception as exc:  # noqa: BLE001
        log.info("embedding skipped: %s", exc)
    # 3. write (short)
    with session_scope() as s:
        inc2 = s.get(Incident, incident_id)
        if inc2 is None:
            return False
        ctx = make_context(s, cfg)
        if vec is not None:
            similar = _similar(ctx, inc2, text, vec)
            if similar:
                extra.append(similar)
        inc2.summary, inc2.impact, inc2.likely_cause = result.summary, result.impact, result.likely_cause
        inc2.suggested_action, inc2.confidence, inc2.analysis_source = result.suggested_action, result.confidence, result.source
        inc2.evidence = [*result.evidence, *extra]
        if result.source == "ai":
            s.add(IncidentEvent(incident=inc2, kind="analysed", at=ctx.now, message=f"AI analysis: {result.likely_cause.splitlines()[0][:300]}",
                                data={"source": "ai"}))
        # alerts not sent yet carry the AI explanation
        for note in s.scalars(select(Notification).where(Notification.incident_id == incident_id, Notification.status == "pending")):
            note.payload = build_payload(ctx, inc2, note.event)
    return result.source == "ai"


def _evidence_text(incident: Incident, failing: list[Outcome], rules: Analysis) -> str:
    brief = {
        "incident": {"title": incident.title, "scope": incident.scope, "severity": incident.severity},
        "failing_checks": [{"check": o.check, "message": o.message, "actual": o.actual,
                            "expected_low": o.expected_low, "expected_high": o.expected_high} for o in failing],
        "rule_based_report": {"what_happened": rules.summary, "likely_cause": rules.likely_cause,
                              "impact": rules.impact, "suggested_action": rules.suggested_action},
        "evidence": [{"label": e.get("label"), "detail": e.get("detail")} for e in rules.evidence],
    }
    return json.dumps(brief, default=str, ensure_ascii=False)


def _write_up(project_name: str, incident: Incident, failing: list[Outcome], rules: Analysis, llm: Any) -> Analysis:
    """A short plain-language briefing on top of the rules' exact report. Rejected if it states anything we can't
    find in the evidence: an unknown number, a quoted name that doesn't exist, or any link."""
    from .llm import invented_numbers

    evidence = _evidence_text(incident, failing, rules)
    out, res = llm.chat_json(SYSTEM_PROMPT, f"Project: {project_name}\nEvidence (JSON):\n{evidence}", max_tokens=1200)
    brief = " ".join(str(out.get("briefing") or "").split())
    if not brief or len(brief) < 40:
        raise ValueError("the model's briefing was empty")
    problems = []
    if made_up := invented_numbers(brief, evidence):
        problems.append(f"numbers not in the evidence: {sorted(made_up)[:5]}")
    unknown = [q for q in _QUOTED.findall(brief) if q.lower() not in evidence.lower()]
    if unknown:
        problems.append(f"names not in the evidence: {unknown[:4]}")
    if "http" in brief.lower():
        problems.append("contains a link")
    if problems:
        raise ValueError("; ".join(problems))
    return Analysis(
        summary=f"{brief}\n\n{rules.summary}", impact=rules.impact, likely_cause=rules.likely_cause,
        suggested_action=rules.suggested_action, confidence=rules.confidence,
        evidence=[*rules.evidence, {"label": "AI briefing", "tool": "ai", "data": {"model": res.model, "tried": res.tried},
                                    "detail": f"The first paragraph was written by {res.model} (tried {', '.join(res.tried)}) "
                                              "from the evidence and checked: every number and name it uses appears in the evidence."}],
        source="ai",
    )


# 'quoted names' only: an apostrophe inside a word (Site A's, today's) isn't a quote
_QUOTED = re.compile(r"(?<![A-Za-z0-9])'([^'\n]{2,80}?)'(?![A-Za-z0-9])")


TRIAGE = {
    "escalate": {"type": "noul", "instructions": "Does this data incident need a person to act today?",
                 "criteria": {"true": "Data people rely on is missing or wrong right now.",
                              "false": "Cosmetic, already recovered, or no business impact."}},
    "urgency": {"type": "score", "instructions": "How urgent is it?",
                "criteria": ["Can wait until next week", "Fix this week", "Fix today", "Drop everything now"]},
}
URGENCY = ["can wait until next week", "fix this week", "fix today", "drop everything now"]


def _triage(incident: Incident, a: Analysis, llm: Any) -> dict[str, Any]:
    state = {"incident": incident.title, "severity_by_rules": incident.severity,
             "what_happened": a.summary[:1200], "impact": a.impact[:600], "likely_cause": a.likely_cause[:600]}
    ans = llm.decide(TRIAGE, state)
    esc = float((ans.get("escalate") or {}).get("noul", 0.0))
    urg = ans.get("urgency") or {}
    probs = {int(k): float(v) for k, v in (urg.get("probabilities") or {}).items()}
    top = max(probs, key=lambda k: probs[k]) if probs else None
    label = URGENCY[top] if top is not None and 0 <= top < len(URGENCY) else "unknown"
    detail = (f"Needs action today: {'yes' if esc >= 0.5 else 'no'} ({esc:.0%} sure). "
              f"Urgency: {label}" + (f" ({probs[top]:.0%})" if top is not None else "") + ".")
    return {"label": "AI triage", "detail": detail, "tool": "triage",
            "data": {"escalate": round(esc, 4), "urgency": label, "urgency_score": urg.get("score"),
                     "probabilities": {URGENCY[k]: round(v, 4) for k, v in probs.items() if 0 <= k < len(URGENCY)}}}


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _similar(ctx: ProjectContext, incident: Incident, text: str, vec: list[float]) -> dict[str, Any] | None:
    rows = list(ctx.session.scalars(select(IncidentEmbedding).where(IncidentEmbedding.project_id == ctx.project.id,
                                                                    IncidentEmbedding.incident_id != incident.id)))
    mine = ctx.session.scalars(select(IncidentEmbedding).where(IncidentEmbedding.incident_id == incident.id)).first()
    if mine is None:
        ctx.session.add(IncidentEmbedding(incident_id=incident.id, project_id=ctx.project.id, text=text[:1000],
                                          vector=vec, model=load_ai_config().embedding_model))
    else:
        mine.text, mine.vector = text[:1000], vec
    scored = sorted(((_cos(vec, r.vector), r) for r in rows), key=lambda x: -x[0])
    lines = []
    for score, r in scored[:3]:
        if score < 0.8:
            break
        past = ctx.session.get(Incident, r.incident_id)
        if past is None:
            continue
        took = (f", fixed in {(past.resolved_at - past.opened_at).total_seconds() / 3600:.0f} h" if past.resolved_at else ", still open")
        lines.append(f"#{past.id} {past.title} ({past.opened_at:%d %b}{took}; {score:.0%} similar)")
    return {"label": "Similar past incidents", "detail": "; ".join(lines), "tool": "embeddings"} if lines else None
