"""The assistant's two endpoints: a conversation turn, and doing what the person confirmed.

Confirming re-checks everything from scratch (the incident still exists and is in that state, the person is on that
project, owners-only actions need an owner) and then runs the very same code as the app's own buttons."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..ai.agent import Refusal, propose, respond, span_label, world_of
from ..ai.usage import tagged
from ..db import Incident, IncidentEvent
from ..timeutil import utcnow
from .auth import SessionDep, UserDep, project_access

router = APIRouter()


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default_factory=list, max_length=20)
    focus: int | None = None  # the incident the conversation is about
    project: str | None = Field(default=None, max_length=100)  # the project page they're on


@router.post("/api/bot/chat")
def chat(body: ChatIn, user: UserDep, session: SessionDep) -> dict:
    session.commit()  # nothing held while a model thinks
    with tagged(user_id=user.id, project=body.project, feature="ask"):
        r = respond(session, user, body.message.strip(), history=[t.model_dump() for t in body.history], focus=body.focus,
                    current=body.project)
    return r.as_dict()


class ActIn(BaseModel):
    tool: str = Field(max_length=40)
    args: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/bot/act")
def act(body: ActIn, request: Request, user: UserDep, session: SessionDep) -> dict:
    """Do one confirmed thing. Every check is made again here: what the client sends is only a request."""
    w = world_of(session, user, body.args.get("project"))
    try:
        p = propose(body.tool, body.args, w)
    except Refusal as r:
        raise HTTPException(409, str(r)) from None
    if not p["allowed"]:
        raise HTTPException(403, p["why_not"] or "not allowed")
    a = p["args"]
    from . import main

    def access(key: str):  # noqa: ANN202
        return project_access(user, session, key)

    def inc_project(iid: int) -> str:
        i = w.incident(iid)
        assert i is not None
        return i["project"]

    done: list[str] = []
    links: list[dict[str, str]] = []
    try:
        if body.tool == "acknowledge":
            for iid in a["incidents"]:
                main.acknowledge(access(inc_project(iid)), session, iid)
                done.append(f"#{iid}")
            msg = f"Acknowledged {', '.join(done)}. They no longer count as needing attention; they stay open until resolved."
        elif body.tool == "resolve":
            for iid in a["incidents"]:
                main.resolve(access(inc_project(iid)), session, iid, main.ResolveIn(note=a.get("note")))
                done.append(f"#{iid}")
            msg = f"Resolved {', '.join(done)}{' with your note' if a.get('note') else ''}. If the same checks still fail, I'll open a new incident."
        elif body.tool == "comment":
            main.comment(access(inc_project(a["incident"])), session, a["incident"], main.CommentIn(text=a["text"]))
            msg = f"Added your comment to #{a['incident']}."
        elif body.tool == "accept_normal":
            main.accept_normal(access(inc_project(a["incident"])), session, a["incident"])
            msg = f"Done: from now on I learn from the newer runs for #{a['incident']}, and it resolves once they look alike."
        elif body.tool == "send_alert":
            msg = _send_alert(session, access(inc_project(a["incident"])), a["incident"])
        elif body.tool == "mute":
            ac = access(a["project"])
            main.add_mute(ac, session, main.MuteIn(scope=a["scope"], hours=a["hours"], reason=a["reason"]))
            span = span_label(a["hours"])
            msg = f"Muted {span if span.startswith('until') else 'for ' + span}. I keep watching; Teams just won't hear about it."
            links.append({"label": "See mutes", "to": f"/p/{a['project']}/alerts"})
        elif body.tool == "unmute":
            for mid in a["mutes"]:
                m = next(x for x in w.mutes if x["id"] == mid)
                main.end_mute(access(m["project"]), session, mid)
            msg = "Unmuted. Alerts flow to Teams again."
        elif body.tool == "send_report":
            r = main.morning_report_send(access(a["project"]), session)
            d = r.get("delivery") or {}
            msg = ("Sent today's morning report to Teams." if d.get("sent")
                   else "The report is ready, but no Teams flow is configured for this project, so it wasn't delivered." if d.get("dry_run")
                   else "Teams didn't accept the report yet; I'll keep retrying." if d.get("retrying") or d.get("failed")
                   else "The report couldn't be sent right now.")
            links.append({"label": "Open the report", "to": f"/p/{a['project']}/report"})
        elif body.tool == "run_checks":
            r = main.check_now(user, session, project=a.get("project"))
            parts = [f"{x['name']}: {x['opened']} new, {x['resolved']} resolved" if x["status"] != "error" else f"{x['name']}: couldn't finish ({x['error']})"
                     for x in r["projects"]]
            msg = "Checked. " + "; ".join(parts) + "."
        elif body.tool == "refresh":
            r = main.refresh_everything(request, user, session)
            changed = [f"{s['name']}: removed {', '.join(s['removed'])}" for s in r["sources"] if s.get("removed")] + \
                      [f"{s['name']}: new {', '.join(s['added'])}" for s in r["sources"] if s.get("added")]
            c = r["checks"]
            msg = "Up to date with everything you're connected to. " + ("; ".join(changed) + ". " if changed else "Nothing changed in Azure. ") + \
                  f"Checks: {c['resolved']} fixed, {c['opened']} new, {c['ongoing']} still open."
        else:
            raise HTTPException(400, "unknown action")
    except HTTPException as exc:
        raise HTTPException(exc.status_code, f"{p['title']}: {exc.detail}") from None
    for iid in a.get("incidents") or ([a["incident"]] if a.get("incident") else []):
        links.append({"label": f"Open #{iid}", "to": f"/p/{inc_project(iid)}/i/{iid}"})
    return {"ok": True, "message": msg, "links": links[:3], "title": p["title"]}


def _send_alert(session: SessionDep, access: Any, incident_id: int) -> str:
    """The incident's alert card, to Teams, now (also recorded on its timeline)."""
    from ..engine.notify import dispatch, enqueue
    from . import views

    inc = session.get(Incident, incident_id)
    if inc is None or inc.project_id != access.project.id:
        raise HTTPException(404, "incident not found")
    session.commit()
    ctx = views._ctx(session, access.project, access.cfg, utcnow())
    ev = IncidentEvent(incident=inc, kind="notified", at=ctx.now, message=f"Sent to Teams by {access.user.name}.", data={"by": access.user.name})
    session.add(ev)
    session.flush()
    note = enqueue(ctx, inc, "incident.opened" if inc.status == "open" else "incident.worsened", ev)
    note.payload = {**note.payload, "resent_by": access.user.name}
    session.flush()
    counts = dispatch(ctx)
    session.commit()
    if counts["sent"]:
        return f"Sent #{incident_id} to {access.project.display_name}'s Teams channel."
    if counts["dry_run"]:
        return f"#{incident_id} was prepared, but no Teams flow is configured for {access.project.display_name}, so it wasn't delivered."
    if counts["muted"]:
        return f"#{incident_id} is muted, so it wasn't sent."
    return f"Teams didn't accept #{incident_id} yet; I'll keep retrying."
