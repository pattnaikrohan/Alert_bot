"""Incident grouping and lifecycle.

Failing checks that share a scope (one site, or one output table) become a
single incident, e.g. "row count low" + "price nulls" on Site A's run →
"Site A scrape incomplete".

    no open incident for the scope, failures now  → open    (notify)
    open, severity up or a new check failing      → worsen  (notify, re-analyse)
    open, fewer checks failing                    → improve (timeline only)
    open, scope evaluated and everything passes   → resolve (notify)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from ..ai.investigator import investigate
from ..db import CheckResult, Incident, IncidentEvent
from .checks import SEVERITY_RANK, Outcome
from .context import ProjectContext
from .notify import enqueue

log = logging.getLogger(__name__)

ACTIVE = ("open", "acknowledged")
BOT = "Alert Bot"  # who resolved it, when the bot did (its checks pass again, or what they watched is gone)
HOLD = timedelta(hours=24)  # a person's "resolved" stands this long against the very same failure


def evidence_key(check: str, run_ref: str | None, details: dict | None) -> str:
    """What a failure is about: the check and the run it saw (a missed schedule: the last run it saw)."""
    return f"{check}|{run_ref or (details or {}).get('last_run_at') or ''}"


def _held_by_person(ctx: ProjectContext, fingerprint: str, failing: list[Outcome], severity: str) -> Incident | None:
    """Someone resolved this scope while it was still failing ("rescheduled", "expected"): the same failure, no worse,
    within a day, doesn't reopen it (and doesn't alert again). New evidence (another run), a worse severity, or the
    same failure a day later does."""
    last = ctx.session.scalars(select(Incident).where(Incident.project_id == ctx.project.id, Incident.fingerprint == fingerprint,
                                                      Incident.status == "resolved").order_by(Incident.resolved_at.desc()).limit(1)).first()
    if last is None or not last.resolved_by or last.resolved_by == BOT or last.resolved_at is None or ctx.now - last.resolved_at > HOLD:
        return None
    if SEVERITY_RANK[severity] > SEVERITY_RANK.get(last.severity, 0):
        return None
    seen = next((set(e.data.get("evidence") or []) for e in reversed(last.events) if e.kind == "resolved" and e.data), set())
    now = {evidence_key(o.check, o.run_ref, o.details) for o in failing}
    return last if seen and now <= seen else None


@dataclass
class IncidentChanges:
    opened: int = 0
    worsened: int = 0
    resolved: int = 0
    ongoing: int = 0


def _feed_title(name: str, failing: list[Outcome]) -> str:
    checks = {o.check for o in failing}
    rc = next((o for o in failing if o.check == "row_count"), None)
    for check, text in (
        ("producer_failed", "producer failing"),
        ("missed_run", "missed a scheduled run"),
        ("freshness", "has stopped writing"),
        ("null_spike", "rows arriving with empty fields"),
    ):
        if check in checks:
            return f"{name} {text}"
    if rc is not None:
        return f"{name} volume unusually {'low' if rc.details.get('direction') == 'low' else 'high'}"
    if "duplicates" in checks:
        return f"{name} writing duplicate rows"
    return f"{name} checks failing"


def _adf_title(name: str, failing: list[Outcome]) -> str:
    checks = {o.check for o in failing}
    rc = next((o for o in failing if o.check == "row_count"), None)
    if "run_failed" in checks:
        return f"{name} pipeline failed"
    if "empty_site" in checks:
        return f"{name} copied no rows"
    if rc is not None:
        return f"{name} copied unusually {'few' if rc.details.get('direction') == 'low' else 'many'} rows"
    if "missed_run" in checks:
        return f"{name} missed its schedule"
    if "sla_risk" in checks and "run_too_slow" not in checks:
        return f"{name} is likely to finish late"
    if "run_too_slow" in checks:
        return f"{name} running slow"
    return f"{name} checks failing"


def _title(scope: str, failing: list[Outcome], feeds: bool = False, adf: bool = False) -> str:
    kind, _, name = scope.partition(":")
    checks = {o.check for o in failing}
    if kind == "output":
        return f"{name.upper() if feeds else name} table schema changed"
    if kind == "data":
        rc = next((o for o in failing if o.check == "row_count"), None)
        for check, text in (("freshness", "data is stale"), ("null_spike", "has empty values"), ("duplicates", "has duplicate rows"),
                            ("business_rule", "breaks a business rule"), ("referential_integrity", "has rows with no match"),
                            ("value_range", "has unexpected negative values"), ("distribution_shift", "data distribution changed"),
                            ("schema_drift", "schema changed")):
            if check in checks and not (rc is not None and rc.details.get("direction") == "low" and check != "freshness"):
                return f"{name} {text}"
        if rc is not None:
            return f"{name} has unusually {'few' if rc.details.get('direction') == 'low' else 'many'} rows today"
        return f"{name} data quality checks failing"
    if kind == "table":
        rc = next((o for o in failing if o.check == "row_count"), None)
        if "empty_site" in checks:
            return f"{name} received no rows"
        if rc is not None:
            return f"{name} loaded unusually {'few' if rc.details.get('direction') == 'low' else 'many'} rows"
        if "rows_skipped" in checks:
            return f"{name} load skipped rows"
        return f"{name} checks failing"
    if adf:
        return _adf_title(name, failing)
    if feeds:
        return _feed_title(name, failing)
    rc = next((o for o in failing if o.check == "row_count"), None)
    for check, text in (
        ("run_failed", "scrape failed"),
        ("empty_site", "returned no data"),
        ("null_spike", "scrape incomplete"),
    ):
        if check in checks:
            return f"{name} {text}"
    if rc is not None:
        return f"{name} scrape incomplete" if rc.details.get("direction") == "low" else f"{name} row volume unusually high"
    for check, text in (
        ("stuck_trigger", "trigger stuck"),
        ("mismatch", "trigger and output disagree"),
        ("run_too_slow", "scrape running slow"),
        ("freshness", "data is stale"),
        ("duplicates", "duplicate rows"),
    ):
        if check in checks:
            return f"{name} {text}"
    return f"{name} checks failing"


def _worst(failing: list[Outcome]) -> str:
    return max((o.severity or "warning" for o in failing), key=lambda s: SEVERITY_RANK[s])


def _event(ctx: ProjectContext, incident: Incident, kind: str, message: str, **data) -> IncidentEvent:  # noqa: ANN003
    ev = IncidentEvent(incident=incident, kind=kind, message=message, data=data, at=ctx.now)
    ctx.session.add(ev)
    return ev


def _apply_analysis(ctx: ProjectContext, incident: Incident, failing: list[Outcome]) -> None:
    a = investigate(ctx, incident, failing)
    incident.summary, incident.impact = a.summary, a.impact
    incident.likely_cause, incident.suggested_action = a.likely_cause, a.suggested_action
    incident.confidence, incident.analysis_source, incident.evidence = a.confidence, a.source, a.evidence
    _event(ctx, incident, "analysed", f"{'AI' if a.source == 'ai' else 'Rule-based'} analysis: {a.likely_cause}",
           source=a.source, confidence=a.confidence)


def process(ctx: ProjectContext, outcomes: list[Outcome], results: dict[int, CheckResult], alert: bool = True) -> IncidentChanges:
    """`results` maps id(outcome) → its stored CheckResult row, to link results to incidents."""
    changes = IncidentChanges()
    failing_by_scope: dict[str, list[Outcome]] = defaultdict(list)
    evaluated: set[str] = set()
    for o in outcomes:
        if o.status in ("pass", "fail"):
            evaluated.add(o.scope)
        if o.failed:
            failing_by_scope[o.scope].append(o)

    active = {
        i.fingerprint: i
        for i in ctx.session.scalars(
            select(Incident).where(Incident.project_id == ctx.project.id, Incident.status.in_(ACTIVE))
        )
    }

    for scope, failing in failing_by_scope.items():
        fingerprint = f"{ctx.key}:{scope}"
        severity = _worst(failing)
        checks = sorted({o.check for o in failing})
        run_ref = next((o.run_ref for o in failing if o.run_ref), None)
        incident = active.get(fingerprint)

        if incident is None and _held_by_person(ctx, fingerprint, failing, severity) is not None:
            changes.ongoing += 1  # resolved by a person against exactly this failure: their call stands for now
            continue
        if incident is None:
            incident = Incident(
                project_id=ctx.project.id, fingerprint=fingerprint, scope=scope, title=_title(scope, failing, ctx.cfg.kind == "feeds", ctx.cfg.kind == "adf"),
                severity=severity, checks=checks, run_ref=run_ref, opened_at=ctx.now, updated_at=ctx.now,
                last_seen_at=ctx.now, status="open",
            )
            ctx.session.add(incident)
            ctx.session.flush()
            ev = _event(ctx, incident, "opened", f"Opened as {severity}: {', '.join(checks)}.", checks=checks, severity=severity)
            _apply_analysis(ctx, incident, failing)
            ctx.session.flush()
            if alert:
                enqueue(ctx, incident, "incident.opened", ev)
            changes.opened += 1
        else:
            prev_rank = SEVERITY_RANK[incident.severity]
            new_checks = sorted(set(checks) - set(incident.checks))
            gone = sorted(set(incident.checks) - set(checks))
            incident.last_seen_at = ctx.now
            if SEVERITY_RANK[severity] > prev_rank or new_checks:
                why = []
                if SEVERITY_RANK[severity] > prev_rank:
                    why.append(f"severity {incident.severity} → {severity}")
                if new_checks:
                    why.append(f"now also failing: {', '.join(new_checks)}")
                incident.severity, incident.checks, incident.updated_at = severity, checks, ctx.now
                incident.title = _title(scope, failing, ctx.cfg.kind == "feeds", ctx.cfg.kind == "adf")
                incident.run_ref = run_ref or incident.run_ref
                if incident.status == "acknowledged":  # what was acknowledged isn't what's happening now: it needs a look again
                    incident.status = "open"
                    why.append(f"it needs attention again (acknowledged earlier by {incident.acknowledged_by or 'someone'})")
                ev = _event(ctx, incident, "worsened", "Got worse: " + "; ".join(why) + ".", checks=checks, severity=severity)
                _apply_analysis(ctx, incident, failing)
                ctx.session.flush()
                if alert:
                    enqueue(ctx, incident, "incident.worsened", ev)
                changes.worsened += 1
            else:
                if gone:
                    incident.checks, incident.updated_at = checks, ctx.now
                    _event(ctx, incident, "improved", f"No longer failing: {', '.join(gone)}.", checks=checks)
                changes.ongoing += 1

        for o in failing:
            row = results.get(id(o))
            if row is not None:
                row.incident_id = incident.id

    for incident in active.values():
        if incident.scope in failing_by_scope or incident.scope not in evaluated:
            continue
        incident.status, incident.resolved_at, incident.updated_at = "resolved", ctx.now, ctx.now
        incident.resolved_by, incident.resolution = BOT, "All checks for this scope pass again."
        ev = _event(ctx, incident, "resolved", "All checks for this scope pass again.", by=BOT)
        ctx.session.flush()
        if alert:
            enqueue(ctx, incident, "incident.resolved", ev)
        changes.resolved += 1

    ctx.session.flush()
    return changes
