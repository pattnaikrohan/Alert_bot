"""Fixing a failure, step by step, and learning whether it worked.

    POST /api/projects/{key}/fix                  the fix for an incident's (or a run's) error: the steps to follow now,
                                                  what was tried before, how often this fix has worked
    POST /api/projects/{key}/fix/{id}/feedback    "did it work?" yes (with what they did) or no (with why) → the next approach
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..ai import fixes as F
from ..db import CheckResult, FixAttempt, Incident, Run
from ..timeutil import iso, utcnow
from .auth import AccessDep, SessionDep

router = APIRouter()


class FixIn(BaseModel):
    incident_id: int | None = None
    run_id: str | None = Field(default=None, max_length=128)


class FeedbackIn(BaseModel):
    worked: bool
    reason: str | None = Field(default=None, max_length=1500)  # why it didn't work
    note: str | None = Field(default=None, max_length=1500)  # what they did, if it worked


def _failure(session: SessionDep, access: Any, body: FixIn) -> tuple[str, Run, Incident | None]:
    """The run whose error we're fixing, and the incident it belongs to (if any)."""
    inc = None
    run = None
    if body.incident_id is not None:
        inc = session.get(Incident, body.incident_id)
        if inc is None or inc.project_id != access.project.id:
            raise HTTPException(404, "incident not found")
        refs = [inc.run_ref] + [r.details.get("run_id") or r.run_ref for r in session.scalars(
            select(CheckResult).where(CheckResult.incident_id == inc.id, CheckResult.status == "fail").order_by(CheckResult.created_at.desc()))]
        for ref in [r for r in refs if r]:
            run = session.scalars(select(Run).where(Run.project_id == access.project.id, (Run.run_id == ref) | (Run.trigger_id == ref))).first()
            if run is not None and run.error:
                break
        key = f"incident:{inc.id}"
    elif body.run_id:
        run = session.scalars(select(Run).where(Run.project_id == access.project.id, Run.run_id == body.run_id)).first()
        key = f"run:{body.run_id}"
    else:
        raise HTTPException(422, "say which incident or run")
    if run is None or not run.error:
        raise HTTPException(404, "no error to fix here (this problem isn't a failed run)")
    return key, run, inc


def _attempt(r: FixAttempt) -> dict[str, Any]:
    return {"id": r.id, "variant": r.variant, "source": r.source, "title": r.title, "steps": r.steps, "verify": r.verify, "status": r.status,
            "reason": r.reason, "note": r.note, "created_at": iso(r.created_at), "created_by": r.created_by,
            "answered_at": iso(r.answered_at), "answered_by": r.answered_by}


def _next(session: SessionDep, access: Any, key: str, run: Run, inc: Incident | None, previous: list[FixAttempt], reason: str = "") -> FixAttempt | None:
    """Choose the next approach and record it as an open attempt (None when there's nothing left to try)."""
    sig, known = F.signature_of(run.error or "")
    ctx = F.context_of(run.error or "", run.site)
    tried = {a.variant for a in previous}
    everyone = list(session.scalars(select(FixAttempt).where(FixAttempt.signature == sig)))
    stats = F.stats_for(everyone)
    pick: dict[str, Any] | None = None
    # what the team found worked for this error before (steps the library didn't have) comes first
    team = sorted((a for a in everyone if a.status == "worked" and a.source in ("ai", "team") and f"team:{a.id}" not in tried
                   and a.key != key), key=lambda a: a.answered_at or a.created_at, reverse=True)
    if team and not reason:
        t = team[0]
        pick = {"variant": f"team:{t.id}", "source": "team", "title": f"What worked for your team: {t.title}", "steps": t.steps, "verify": t.verify}
    if pick is None and known is not None:
        v = F.pick_variant(known, tried, stats, reason)
        if v is not None:
            pick = {"variant": v.key, "source": "library", "title": v.name, "steps": F.steps_of(v, ctx), "verify": F.render(v.verify, ctx)}
    if pick is None:
        session.commit()  # nothing held while the model thinks
        history = [{"title": a.title, "steps": a.steps, "reason": a.reason} for a in previous]
        ai = F.ai_steps(run.error or "", ctx, history)
        if ai is not None:
            n = sum(1 for a in previous if a.source == "ai") + 1
            pick = {"variant": f"ai:{n}", "source": "ai", "title": ai["title"], "steps": ai["steps"], "verify": ai["verify"]}
    if pick is None and "escalate" not in tried:
        pick = {"variant": "escalate", "source": "escalate", "title": F.ESCALATE.name, "steps": F.steps_of(F.ESCALATE, ctx),
                "verify": F.ESCALATE.verify}
    if pick is None:
        return None
    a = FixAttempt(project_id=access.project.id, key=key, incident_id=inc.id if inc else None, run_id=run.run_id, pipeline=run.site,
                   signature=sig, parent_id=previous[-1].id if previous else None, created_by=access.user.name, **pick)
    session.add(a)
    session.flush()
    return a


def view(session: SessionDep, access: Any, key: str, run: Run, inc: Incident | None, *, celebrate: bool = False) -> dict[str, Any]:
    sig, known = F.signature_of(run.error or "")
    ctx = F.context_of(run.error or "", run.site)
    mine = list(session.scalars(select(FixAttempt).where(FixAttempt.project_id == access.project.id, FixAttempt.key == key).order_by(FixAttempt.id)))
    current = mine[-1] if mine else None
    everyone = list(session.scalars(select(FixAttempt).where(FixAttempt.signature == sig)))
    stats = F.stats_for(everyone)
    worked = sum(1 for a in everyone if a.status == "worked")
    failed = sum(1 for a in everyone if a.status == "failed")
    since = current.created_at if current else None
    ok_after = session.scalars(select(Run).where(Run.project_id == access.project.id, Run.site == run.site, Run.run_status == "Succeeded",
                                                 Run.requested_at > (since or run.requested_at)).order_by(Run.requested_at)).first()
    ai = next((a for a in mine if a.source == "ai"), None)
    return {
        "applicable": True, "signature": sig, "code": ctx.get("code"), "known": known is not None,
        "title": known.title if known else (ai.title if ai else "An error the library doesn't know yet"),
        "meaning": F.render(known.meaning, ctx) if known else "The bot's library has no entry for this error, so the steps below are the AI's, "
                   "written from the error itself. They're marked as not yet proven until someone confirms they worked.",
        "source": known.source if known else "AI", "docs": known.docs if known else [
            {"label": "Search Microsoft Q&A for this error", "url": F.render("{qa_link}", ctx)}],
        "pipeline": run.site, "step": ctx.get("step"), "run_id": run.run_id, "run_at": iso(run.requested_at),
        "error": (run.error or "")[:1200],
        "current": _attempt(current) if current else None,
        "history": [_attempt(a) for a in mine[:-1]],
        "current_stats": dict(zip(("worked", "failed"), stats.get(current.variant, (0, 0)), strict=True)) if current else None,
        "totals": {"worked": worked, "failed": failed, "approaches": len(known.variants) if known else None},
        "team_notes": [{"by": a.answered_by, "at": iso(a.answered_at), "note": a.note, "approach": a.title}
                       for a in sorted(everyone, key=lambda a: a.answered_at or a.created_at, reverse=True) if a.status == "worked" and a.note][:3],
        "next_run_ok": {"at": iso(ok_after.requested_at), "run_id": ok_after.run_id} if ok_after and current and current.status == "open" else None,
        "incident": {"id": inc.id, "status": inc.status} if inc else None,
        "celebrate": celebrate,
    }


@router.post("/api/projects/{key}/fix")
def fix(body: FixIn, access: AccessDep, session: SessionDep) -> dict:
    key, run, inc = _failure(session, access, body)
    mine = list(session.scalars(select(FixAttempt).where(FixAttempt.project_id == access.project.id, FixAttempt.key == key).order_by(FixAttempt.id)))
    if not mine:
        _next(session, access, key, run, inc, [])
        session.commit()
    return view(session, access, key, run, inc)


@router.post("/api/projects/{key}/fix/{attempt_id}/feedback")
def feedback(attempt_id: int, body: FeedbackIn, access: AccessDep, session: SessionDep) -> dict:
    a = session.get(FixAttempt, attempt_id)
    if a is None or a.project_id != access.project.id:
        raise HTTPException(404, "not found")
    if a.status != "open":
        raise HTTPException(409, "already answered")
    fixin = FixIn(incident_id=a.incident_id) if a.key.startswith("incident:") else FixIn(run_id=a.run_id)
    key, run, inc = _failure(session, access, fixin)
    a.status, a.answered_by, a.answered_at = ("worked" if body.worked else "failed"), access.user.name, utcnow()
    if body.worked:
        a.note = (body.note or "").strip() or None
        session.commit()
        return view(session, access, key, run, inc, celebrate=True)
    a.reason = (body.reason or "").strip() or None
    session.flush()
    previous = list(session.scalars(select(FixAttempt).where(FixAttempt.project_id == access.project.id, FixAttempt.key == key).order_by(FixAttempt.id)))
    _next(session, access, key, run, inc, previous, a.reason or "")
    session.commit()
    return view(session, access, key, run, inc)
