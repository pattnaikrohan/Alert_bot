"""FastAPI app: the portal's only door to the metadata DB.

The portal never runs checks; it reads what the bot stored. The few writes
here are human actions: acknowledge, mute, accept a schema change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import __version__
from ..config import load_projects
from ..db import Incident, IncidentEvent, Mute, Project, UserProject, create_all
from ..settings import get_settings
from ..timeutil import iso, utcnow
from . import ai_usage, bot, charts, compute, cortex, databases, fixes, pipeline_report, storage, suspicious, views
from .auth import AccessDep, OwnerDep, SessionDep, UserDep

settings = get_settings()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    create_all()
    stop = None
    if settings.embedded_worker:  # one process: the worker shares the server's in-memory Microsoft sign-in
        from ..engine.worker import start_in_thread

        stop = start_in_thread(settings.worker_tick)
    yield
    if stop:
        stop.set()


app = FastAPI(title="Alert Bot API", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json",
              lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.portal_url, "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(databases.router)
app.include_router(charts.router)  # charts people ask the bot for, pinned per person
app.include_router(bot.router)  # the assistant: questions, and the app driven in plain English
app.include_router(fixes.router)  # step-by-step fixes for failures, learning from whether they worked
app.include_router(storage.router)  # warehouse cost: unused tables, what they cost, the script to remove them
app.include_router(pipeline_report.router)  # the pipeline report: runs, volumes and errors per day, today against usual, insights
app.include_router(compute.router)  # Snowflake compute cost: spend, waste, savings scripts, alerts
app.include_router(cortex.router)  # Snowflake Cortex AI usage: the AI lines of the bill, by product, user, model
app.include_router(suspicious.router)  # suspicious values: rules from column names (the rules bot), counts, and values for the owner
app.include_router(ai_usage.router)  # AI tokens and dollars: real usage, priced on any provider


@app.exception_handler(RequestValidationError)
async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Say what's wrong without echoing what was sent: request bodies can hold passwords or connection strings."""
    parts = [f"{'.'.join(str(x) for x in e.get('loc', ()) if x != 'body')}: {e.get('msg', 'invalid')}" for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": "; ".join(parts) or "invalid request"})  # databases people connect + pipeline → table mappings (data quality)


# ── public ────────────────────────────────────────────────────────────────────


@app.get("/api/health")
def health(session: SessionDep) -> dict:
    from ..db import BotCycle

    last = session.scalars(select(BotCycle).order_by(BotCycle.started_at.desc()).limit(1)).first()
    minutes = (utcnow() - last.started_at).total_seconds() / 60 if last else None
    return {
        "status": "ok",
        "version": __version__,
        "auth_mode": settings.auth_mode,
        "bot": {
            "last_cycle_at": iso(last.started_at) if last else None,
            "minutes_since_last_cycle": round(minutes, 1) if minutes is not None else None,
            # The bot cycles every 10 min; 30 min of silence means the watcher itself needs watching.
            "stale": minutes is None or minutes > 30,
        },
    }


@app.get("/api/auth/config")
def auth_config() -> dict:
    s = get_settings()
    return {"mode": s.auth_mode, "mfa": s.auth_mode in ("entra", "microsoft") and s.require_mfa}


# Microsoft sign-in from the portal: a code to enter at microsoft.com/devicelogin, then a session cookie.

MAX_PENDING_SIGN_INS = 25


@app.post("/api/auth/device/start")
def device_start() -> dict:
    from .. import msauth

    if get_settings().auth_mode != "microsoft":
        raise HTTPException(404, "Microsoft sign-in is not enabled")
    if sum(f.status == "waiting" for f in list(msauth._flows.values())) >= MAX_PENDING_SIGN_INS:
        raise HTTPException(429, "Too many sign-ins in progress; try again in a minute.")
    try:
        flow = msauth.start_flow()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Couldn't reach Microsoft sign-in: {exc}") from exc
    return {"flow_id": flow.id, "user_code": flow.user_code, "verification_uri": flow.verification_uri,
            "expires_at": int(flow.expires_at)}


@app.get("/api/auth/device/{flow_id}")
def device_poll(flow_id: str, session: SessionDep, response: Response) -> dict:
    from .. import msauth
    from .auth import COOKIE, create_web_session, link_user

    flow = msauth.get_flow(flow_id)
    if flow is None:
        raise HTTPException(404, "This sign-in has ended; start again.")
    if flow.status != "done":
        return {"status": flow.status, "message": flow.message}
    msauth.finish_flow(flow_id)
    who = flow.identity
    if get_settings().require_mfa and not who.get("mfa"):
        return {"status": "error", "message": "mfa_required: this portal needs Microsoft Authenticator (multi-factor) "
                                              "sign-in, and this sign-in didn't include it."}
    user = link_user(session, who)
    token = create_web_session(session, user, mfa=bool(who.get("mfa")), azure_account=who.get("home_account_id"))
    connected = False
    if user.is_admin and who.get("home_account_id") and not msauth.bot_connected():
        msauth.set_bot_account(who, set_by=user.email)  # the bot reads Data Factory with this admin's sign-in
        connected = True
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=get_settings().portal_url.startswith("https"),
                        max_age=get_settings().session_days * 86400, path="/")
    discovering = False
    if who.get("home_account_id"):
        from ..discovery import discover_in_background

        session.commit()  # the lookup runs in its own session and needs this person to exist
        discover_in_background(user.id, who["home_account_id"])  # what can they see in Azure?
        discovering = True
    return {"status": "done", "user": {"email": user.email, "name": user.name, "admin": user.is_admin},
            "bot_connected_to_azure": connected, "discovering": discovering}


@app.post("/api/auth/logout")
def logout(request: Request, session: SessionDep, response: Response) -> dict:
    from .auth import COOKIE, end_web_session

    end_web_session(session, request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"status": "signed_out"}


@app.post("/api/azure/discover")
def azure_discover(request: Request, user: UserDep, session: SessionDep) -> dict:
    """Look again at what I can see in Azure (Data Factory, Synapse, Logic Apps) with my own sign-in."""
    from ..discovery import discover_user
    from .auth import COOKIE, web_session

    ws = web_session(session, request.cookies.get(COOKIE)) if get_settings().auth_mode == "microsoft" else None
    if ws is None or not ws.azure_account:
        raise HTTPException(400, "Sign in with Microsoft first.")
    hid = ws.azure_account
    session.commit()
    result = discover_user(user.id, hid)
    if "error" in result:
        raise HTTPException(502, f"Couldn't look in Azure: {result['error']}")
    return result


@app.get("/api/azure")
def azure_status(user: UserDep) -> dict:
    """Admins: which account the bot reads Azure Data Factory with, and whether it still works."""
    from .. import msauth

    if not user.is_admin:
        raise HTTPException(403, "admins only")
    acct = msauth.bot_account()
    return {"connected": msauth.bot_connected(), "username": acct.get("username") if acct else None,
            "set_by": acct.get("set_by") if acct else None, "set_at": acct.get("set_at") if acct else None}


@app.post("/api/azure/connect")
def azure_connect(request: Request, user: UserDep, session: SessionDep) -> dict:
    """Admins: use my own Microsoft sign-in for the bot's Data Factory reads."""
    from .. import msauth
    from .auth import COOKIE, web_session

    if not user.is_admin:
        raise HTTPException(403, "admins only")
    ws = web_session(session, request.cookies.get(COOKIE))
    if ws is None or not ws.azure_account:
        raise HTTPException(400, "Sign in with Microsoft first.")
    msauth.set_bot_account({"home_account_id": ws.azure_account, "email": user.email}, set_by=user.email)
    return azure_status(user)


# ── me ────────────────────────────────────────────────────────────────────────


@app.get("/api/me")
def me(user: UserDep, session: SessionDep) -> dict:
    configured = load_projects()
    links = session.execute(
        select(Project, UserProject.role).join(UserProject, UserProject.project_id == Project.id)
        .where(UserProject.user_id == user.id, Project.active.is_(True)).order_by(Project.display_name)
    ).all()
    from ..db import BotState
    from ..discovery import status as discovery_status

    prefs = session.get(BotState, f"prefs:user:{user.id}")
    return {
        "user": {"email": user.email, "name": user.name, "admin": user.is_admin},
        "projects": [views.project_card(session, p, role) for p, role in links if p.key in configured],
        "discovery": discovery_status(session, user.id),
        "prefs": prefs.value if prefs else {},
    }


class Prefs(BaseModel):
    """A person's own settings, kept with their account (not the browser): the dock's order, whether they saw the tour."""

    dock: dict[str, list[Annotated[str, Field(max_length=40)]]] | None = Field(default=None, max_length=6)
    nav_layout: Literal["dock", "side"] | None = None
    tour_done_at: str | None = Field(default=None, max_length=40)
    tour_version: int | None = Field(default=None, ge=0, le=1000)
    style: Literal["modern", "classic"] | None = None  # the app's look: modern (dock, colour, motion) or classic (calm, formal)
    # the AI usage page: which models to price the same work on
    ai_scenarios: list[Annotated[str, Field(max_length=120)]] | None = Field(default=None, max_length=16)


@app.put("/api/me/prefs")
def save_prefs(body: Prefs, user: UserDep, session: SessionDep) -> dict:
    from ..db import BotState

    key = f"prefs:user:{user.id}"
    row = session.get(BotState, key)
    if row is None:
        row = BotState(key=key, value={})
        session.add(row)
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "dock" in patch and patch["dock"] is not None:
        patch["dock"] = {g: ids[:40] for g, ids in patch["dock"].items()}
    row.value, row.updated_at = {**(row.value or {}), **patch}, utcnow()
    session.flush()
    return row.value


@app.get("/api/pipelines")
def all_pipelines(user: UserDep, session: SessionDep, days: Annotated[int, Query(ge=1, le=90)] = 7) -> dict:
    """Every Data Factory / Synapse pipeline and Logic App in the projects I can see, whether it has run or not."""
    links = session.execute(
        select(Project, UserProject.role).join(UserProject, UserProject.project_id == Project.id)
        .where(UserProject.user_id == user.id, Project.active.is_(True)).order_by(Project.display_name)
    ).all()
    return views.pipelines_view(session, [(p, r) for p, r in links], days)


@app.get("/api/lineage")
def lineage(user: UserDep, session: SessionDep) -> dict:
    """Where data comes from and goes to: files and tables on any platform → pipelines → tables → what reads them."""
    links = session.execute(
        select(Project, UserProject.role).join(UserProject, UserProject.project_id == Project.id)
        .where(UserProject.user_id == user.id, Project.active.is_(True)).order_by(Project.display_name)
    ).all()
    return views.lineage_view(session, [(p, r) for p, r in links])


@app.post("/api/pipelines/refresh")
def refresh_pipelines(user: UserDep, session: SessionDep) -> dict:
    """Re-read the list of pipelines / Logic Apps from Azure now, for my projects (read-only)."""
    from ..db import session_scope
    from ..engine.adf import inventory_of, refresh_inventory
    from ..engine.cycle import make_context

    keys = [p.key for p in session.scalars(select(Project).join(UserProject, UserProject.project_id == Project.id)
                                           .where(UserProject.user_id == user.id, Project.active.is_(True)))]
    session.commit()  # release the request's write; each refresh writes in its own short transaction
    cfgs = load_projects()
    errors = {}
    for key in keys:
        cfg = cfgs.get(key)
        if cfg is None or cfg.kind != "adf":
            continue
        with session_scope() as s:
            refresh_inventory(make_context(s, cfg), force=True)
            if err := inventory_of(s, key).get("error"):
                errors[key] = err
    return {"refreshed": len(keys), "errors": errors}


def _live(session: SessionDep, links: list) -> dict:
    """Run watching: when the bot last looked at Azure, and the latest run events across the person's projects."""
    from ..engine.watch import state_of

    looks, events = [], []
    for p, _ in links:
        st = state_of(session, p.key)
        if st.get("at"):
            looks.append(st["at"])
        events += [{**e, "project": p.key, "project_name": p.display_name} for e in st.get("events", [])]
    events.sort(key=lambda e: e.get("seen_at") or "", reverse=True)
    last = max(looks) if looks else None
    age = (utcnow() - datetime.fromisoformat(last).replace(tzinfo=None)).total_seconds() if last else None
    return {"last_look_at": last, "watching": age is not None and age < 180, "events": events[:12]}


@app.get("/api/feed")
def feed(user: UserDep, session: SessionDep) -> dict:
    """What the bot has to say to this user, across every project they belong to (and only those)."""
    from ..db import BotCycle

    configured = load_projects()
    links = [
        (p, role) for p, role in session.execute(
            select(Project, UserProject.role).join(UserProject, UserProject.project_id == Project.id)
            .where(UserProject.user_id == user.id, Project.active.is_(True)).order_by(Project.display_name)
        ).all() if p.key in configured
    ]
    ids = {p.id: p for p, _ in links}
    active, recent = [], []
    if ids:
        for inc in session.scalars(
            select(Incident).where(Incident.project_id.in_(ids), Incident.status.in_(views.ACTIVE))
        ):
            p = ids[inc.project_id]
            active.append({**views.incident_brief(inc), "project": p.key, "project_name": p.display_name})
        rank = {"critical": 2, "warning": 1, "info": 0}
        active.sort(key=lambda i: i["opened_at"], reverse=True)  # newest first…
        active.sort(key=lambda i: -rank.get(i["severity"], 0))  # …within severity (stable sort)
        for ev, inc in session.execute(
            select(IncidentEvent, Incident).join(Incident, Incident.id == IncidentEvent.incident_id)
            .where(Incident.project_id.in_(ids), IncidentEvent.kind.in_(("opened", "worsened", "resolved", "acknowledged")))
            .order_by(IncidentEvent.at.desc(), IncidentEvent.id.desc()).limit(10)
        ).all():
            p = ids[inc.project_id]
            recent.append({"at": iso(ev.at), "kind": ev.kind, "message": ev.message, "incident_id": inc.id,
                           "by": (ev.data or {}).get("by") or (inc.acknowledged_by if ev.kind == "acknowledged" else None),
                           "title": inc.title, "severity": inc.severity, "project": p.key, "project_name": p.display_name})
    today = []
    if ids:
        from sqlalchemy import or_

        start = views._today_start_utc()
        for inc in session.scalars(select(Incident).where(Incident.project_id.in_(ids), Incident.status == "resolved",
                                                          or_(Incident.opened_at >= start, Incident.resolved_at >= start))
                                   .order_by(Incident.resolved_at.desc()).limit(20)):
            p = ids[inc.project_id]
            today.append({**views.incident_brief(inc), "project": p.key, "project_name": p.display_name})
    last = session.scalars(
        select(BotCycle).where(BotCycle.project_id.in_(ids or [-1])).order_by(BotCycle.started_at.desc()).limit(1)
    ).first()
    minutes = (utcnow() - last.started_at).total_seconds() / 60 if last else None
    severities = {i["severity"] for i in active}
    mood ="critical" if "critical" in severities else "warning" if active else "ok"
    return {
        "user": {"name": user.name, "first_name": user.name.split(" ")[0]},
        "mood": mood,
        "projects": [views.project_card(session, p, role) for p, role in links],
        "active": active,
        "resolved_today": today,
        "recent": recent,
        "live": _live(session, links),
        "bot": {"last_cycle_at": iso(last.started_at) if last else None,
                "stale": minutes is None or minutes > 30,
                # the last round failed outright (e.g. the Azure sign-in expired): the bot should say so
                "error": (last.error or "")[:300] if last and last.status == "error" else None},
    }


# ── talking to the bot ────────────────────────────────────────────────────────


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


def _answer_dict(a: object) -> dict:
    from ..ai.ask import Answer

    assert isinstance(a, Answer)
    return {"answer": a.text, "links": a.links, "mode": a.mode, "topic": a.topic, "model": a.model or None}


@app.post("/api/ask")
def ask(user: UserDep, session: SessionDep, body: AskIn) -> dict:
    """A plain-language question, answered read-only from the asker's own projects."""
    from ..ai.ask import answer
    from ..ai.usage import tagged

    with tagged(user_id=user.id, feature="ask"):
        return _answer_dict(answer(session, user, body.question))


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=1500)


_SPOKEN: dict[str, bytes] = {}  # small cache: the robot says the same lines often


@app.post("/api/speak")
def speak(user: UserDep, body: SpeakIn) -> Response:
    """The robot's voice (AI text-to-speech). 503 when unavailable; the portal then uses the browser's voice."""
    import hashlib

    from ..ai.llm import AIUnavailable, client
    from ..ai.usage import tagged

    key = hashlib.sha1(body.text.encode()).hexdigest()
    audio = _SPOKEN.get(key)
    if audio is None:
        try:
            with tagged(user_id=user.id):
                audio = client().speak(body.text)
        except AIUnavailable as exc:
            raise HTTPException(503, f"voice unavailable: {exc}") from exc
        if len(_SPOKEN) > 200:
            _SPOKEN.pop(next(iter(_SPOKEN)))
        _SPOKEN[key] = audio
    return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/ai")
def ai_status(user: UserDep) -> dict:
    """Which AI models the bot uses, and which are resting after errors."""
    from ..ai.llm import HEALTH
    from ..config import load_ai_config

    ai = load_ai_config()
    return {"enabled": ai.ready, "provider": ai.provider or None, "chat_models": ai.chat_models, "decision_model": ai.decision_model or None,
            "embedding_model": ai.embedding_model or None, "speech_model": ai.speech_model or None, "health": HEALTH.status()}


@app.get("/api/summary")
def summary(user: UserDep, session: SessionDep) -> dict:
    from ..ai.ask import summary as make_summary

    return _answer_dict(make_summary(session, user))


@app.post("/api/check-now")
def check_now(user: UserDep, session: SessionDep, project: str | None = None) -> dict:
    """Run a bot cycle now for the caller's projects (checks are read-only; alerts follow the usual rules)."""
    from ..engine.cycle import run_cycle

    configured = load_projects()
    keys = [
        p.key for p in session.scalars(
            select(Project).join(UserProject, UserProject.project_id == Project.id)
            .where(UserProject.user_id == user.id, Project.active.is_(True))
        ) if p.key in configured and (project is None or p.key == project)
    ]
    if project and not keys:
        raise HTTPException(404, "project not found")
    session.commit()  # release the request's write (last_seen) before the cycles write
    from ..db import session_scope as _scope
    from ..engine.adf import refresh_inventory
    from ..engine.cycle import make_context

    out = []
    for key in keys:
        r = run_cycle(configured[key])
        if configured[key].kind == "adf":
            try:
                with _scope() as s2:
                    refresh_inventory(make_context(s2, configured[key]), force=True)
            except Exception:  # noqa: BLE001 - the runs were still read
                pass
        out.append({"key": key, "name": configured[key].display_name, "status": r.status, "error": r.error,
                    "checks": r.checks, "failed_checks": r.failed_checks, "opened": r.changes.opened,
                    "worsened": r.changes.worsened, "resolved": r.changes.resolved, "ongoing": r.changes.ongoing})
    return {"projects": out}


@app.post("/api/refresh")
def refresh_everything(request: Request, user: UserDep, session: SessionDep) -> dict:
    """Re-read everything this person is connected to, now, and say what changed:
    Azure (which Data Factories, Synapse workspaces and Logic Apps they can see, and each one's pipelines as Azure has
    them now), every database they connected (reachable? each mapped table still there?), then every check (runs,
    Snowflake feeds, data checks). Read-only everywhere."""
    from ..config import load_projects as _load
    from ..db import DbConnection
    from ..db import session_scope as _scope
    from ..dq.service import verify_tables
    from ..engine.adf import inventory_of, refresh_inventory
    from ..engine.cycle import make_context, run_cycle
    from .auth import COOKIE, web_session

    report: dict = {"azure": None, "sources": [], "databases": [], "checks": {"opened": 0, "resolved": 0, "ongoing": 0}}
    # 1. Azure: what this person can see (their own sign-in), so a new or removed factory / workspace / Logic App counts
    ws = web_session(session, request.cookies.get(COOKIE)) if get_settings().auth_mode == "microsoft" else None
    session.commit()
    if ws is not None and ws.azure_account:
        from ..discovery import discover_user

        try:
            r = discover_user(user.id, ws.azure_account)
            report["azure"] = {"status": "error", "error": r["error"]} if "error" in r else \
                {"status": "ok", "found": r.get("found", 0), "joined": r.get("joined", []), "removed": r.get("removed", [])}
        except Exception as exc:  # noqa: BLE001 - the rest still refreshes
            report["azure"] = {"status": "error", "error": str(exc)[:300]}
    configured = _load()
    with _scope() as s:
        keys = [p.key for p in s.scalars(select(Project).join(UserProject, UserProject.project_id == Project.id)
                                         .where(UserProject.user_id == user.id, Project.active.is_(True))) if p.key in configured]
    # 2. each project's source, as it is now
    for key in keys:
        cfg = configured[key]
        src = {"key": key, "name": cfg.display_name, "status": "ok", "error": None, "added": [], "removed": [], "count": None,
               "service": ({"adf": "Data Factory", "synapse": "Synapse", "logic_apps": "Logic Apps"}.get(cfg.pipeline.type, "Azure")
                           if cfg.kind == "adf" and cfg.pipeline else "Snowflake feeds" if cfg.kind == "feeds" else "Pipelines")}
        if cfg.kind == "adf":
            try:
                with _scope() as s:
                    before = {i["name"] for i in inventory_of(s, key).get("items", [])}
                    refresh_inventory(make_context(s, cfg), force=True)
                    inv = inventory_of(s, key)
                    after = {i["name"] for i in inv.get("items", [])}
                if inv.get("error"):
                    src["status"], src["error"] = "error", inv["error"]
                else:
                    src["added"], src["removed"], src["count"] = sorted(after - before), sorted(before - after), len(after)
            except Exception as exc:  # noqa: BLE001
                src["status"], src["error"] = "error", str(exc)[:300]
        report["sources"].append(src)
    # 3. databases this person connected
    with _scope() as s:
        conn_ids = list(s.scalars(select(DbConnection.id).where(DbConnection.user_id == user.id)))
    for cid in conn_ids:
        with _scope() as s:
            c = s.get(DbConnection, cid)
            try:
                report["databases"].append(verify_tables(s, c, utcnow()))
            except Exception as exc:  # noqa: BLE001
                report["databases"].append({"id": c.id, "name": c.name, "kind": c.kind, "status": "error", "error": str(exc)[:300],
                                            "tables": 0, "missing": [], "back": []})
    # 4. every check, on what was just read
    for src in report["sources"]:
        r = run_cycle(configured[src["key"]], force_quality=True)
        if r.status == "error" and src["status"] == "ok":
            src["status"], src["error"] = "error", r.error
        src["checks"] = r.checks
        report["checks"]["opened"] += r.changes.opened
        report["checks"]["resolved"] += r.changes.resolved
        report["checks"]["ongoing"] += r.changes.ongoing + r.changes.worsened
    report["at"] = iso(utcnow())
    return report


# ── project views ─────────────────────────────────────────────────────────────


@app.get("/api/projects/{key}/overview")
def overview(access: AccessDep, session: SessionDep) -> dict:
    return views.overview(session, access.project, access.cfg, access.role)


@app.get("/api/projects/{key}/incidents")
def incidents(
    access: AccessDep, session: SessionDep,
    status: Literal["active", "resolved", "all"] = "all",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    return views.incidents_list(session, access.project, status, limit)


def _incident(session, access, incident_id: int) -> Incident:  # noqa: ANN001
    inc = session.get(Incident, incident_id)
    if inc is None or inc.project_id != access.project.id:  # never leak another project's incident
        raise HTTPException(404, "incident not found")
    return inc


@app.get("/api/projects/{key}/incidents/{incident_id}")
def incident(access: AccessDep, session: SessionDep, incident_id: int) -> dict:
    return views.incident_detail(session, access.project, access.cfg, _incident(session, access, incident_id))


@app.post("/api/projects/{key}/incidents/{incident_id}/acknowledge")
def acknowledge(access: AccessDep, session: SessionDep, incident_id: int) -> dict:
    inc = _incident(session, access, incident_id)
    if inc.status != "open":
        raise HTTPException(409, f"incident is {inc.status}")
    now = utcnow()
    inc.status, inc.acknowledged_at, inc.acknowledged_by, inc.updated_at = "acknowledged", now, access.user.name, now
    session.add(IncidentEvent(incident=inc, kind="acknowledged", message=f"Acknowledged by {access.user.name}.", at=now,
                              data={"by": access.user.name}))
    session.flush()
    return views.incident_detail(session, access.project, access.cfg, inc)


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ResolveIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


@app.post("/api/projects/{key}/incidents/{incident_id}/comments")
def comment(access: AccessDep, session: SessionDep, incident_id: int, body: CommentIn) -> dict:
    """A note on the incident, kept in its record with who wrote it and when (anyone on the project; it can't be edited)."""
    inc = _incident(session, access, incident_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "the comment is empty")
    session.add(IncidentEvent(incident=inc, kind="comment", message=text, at=utcnow(), data={"by": access.user.name}))
    session.flush()
    return views.incident_detail(session, access.project, access.cfg, inc)


@app.post("/api/projects/{key}/incidents/{incident_id}/resolve")
def resolve(access: AccessDep, session: SessionDep, incident_id: int, body: ResolveIn) -> dict:
    """Someone fixed it (or it isn't a problem): close it now, with who and how. If its checks still fail on the bot's
    next look, the bot opens a new incident rather than staying quiet."""
    inc = _incident(session, access, incident_id)
    if inc.status == "resolved":
        raise HTTPException(409, "incident is resolved")
    from ..db import CheckResult
    from ..engine.incidents import evidence_key

    now = utcnow()
    note = (body.note or "").strip() or None
    latest: dict[str, CheckResult] = {}
    for r in session.scalars(select(CheckResult).where(CheckResult.incident_id == inc.id, CheckResult.status == "fail")
                             .order_by(CheckResult.created_at.desc())):
        latest.setdefault(r.check, r)
    evidence = sorted(evidence_key(r.check, r.run_ref, r.details) for r in latest.values())
    inc.status, inc.resolved_at, inc.updated_at = "resolved", now, now
    inc.resolved_by, inc.resolution = access.user.name, note or f"Resolved by {access.user.name}."
    session.add(IncidentEvent(incident=inc, kind="resolved", at=now, data={"by": access.user.name, "manual": True, "evidence": evidence},
                              message=f"Resolved by {access.user.name}" + (f": {note}" if note else ".")))
    session.flush()
    return views.incident_detail(session, access.project, access.cfg, inc)


# which learned measurements each check is about (a "new normal" resets these)
LEARNED = {"row_count": ["row_count"], "empty_site": ["row_count"], "rows_skipped": ["rows_skipped"], "run_too_slow": ["duration_s"],
           "null_spike": ["null_rate", "empty_rate"], "duplicates": ["duplicate_rate"], "freshness": ["freshness_hours"],
           "distribution_shift": ["mean", "top_share"], "value_range": ["negative_rate"], "out_of_range": ["out_of_range_rate"],
           "invalid_type": ["invalid_rate"], "business_rule": ["rule_fail_rate"], "referential_integrity": ["orphan_rate"],
           "missing_partitions": ["days_recent"]}


@app.post("/api/projects/{key}/incidents/{incident_id}/accept-normal")
def accept_normal(access: AccessDep, session: SessionDep, incident_id: int) -> dict:
    """"This is the new normal": from when the incident opened, the bot learns only from the newer runs for what it
    flagged (it stops alerting once they look alike, and keeps learning from every run after). Anyone on the project."""
    from ..db import BaselineReset, CheckResult

    inc = _incident(session, access, incident_id)
    if inc.status == "resolved":
        raise HTTPException(409, "incident is resolved")
    fails = session.scalars(select(CheckResult).where(CheckResult.incident_id == inc.id, CheckResult.status == "fail"))
    targets: set[tuple[str | None, str | None, str]] = set()
    for r in fails:
        kind, _, name = r.scope.partition(":")
        output, site = {"site": (None, name), "table": (f"table:{name}", None), "output": (name, None), "data": (f"dq:{name}", None)}.get(kind, (None, None))
        for metric in LEARNED.get(r.check, []):
            targets.add((output, site, metric))
    if not targets:
        raise HTTPException(409, "this incident isn't about a learned value (a failed run or a schema change can't be 'normal')")
    now = utcnow()
    since = inc.opened_at - timedelta(minutes=1)
    for output, site, metric in sorted(targets, key=str):
        session.add(BaselineReset(project_id=access.project.id, output_id=output, site=site, metric=metric, field=None, since=since,
                                  incident_id=inc.id, created_by=access.user.name))
    if inc.status == "open":
        inc.status, inc.acknowledged_at, inc.acknowledged_by = "acknowledged", now, access.user.name
    inc.updated_at = now
    session.add(IncidentEvent(incident=inc, kind="accepted_normal", at=now,
                              message=f"{access.user.name} marked this as expected: the bot now learns from these runs onwards."))
    session.flush()
    session.commit()
    from .databases import _testing, check_soon

    if not _testing():
        check_soon(access.project.key)  # re-judge now: the incident resolves once the latest values fit the new normal
    return views.incident_detail(session, access.project, access.cfg, inc)


@app.get("/api/projects/{key}/management")
def management(access: AccessDep, session: SessionDep, days: Annotated[int, Query(ge=7, le=90)] = 30) -> dict:
    """The management overview: delivery, reliability, what's broken, trends."""
    return views.management_view(session, access.project, access.cfg, days)


@app.get("/api/projects/{key}/quality")
def quality(access: AccessDep, session: SessionDep, days: Annotated[int, Query(ge=1, le=90)] = 30) -> dict:
    """Data quality inside the tables the pipelines load: each check's latest result and the measured values."""
    if access.cfg.kind != "adf":
        raise HTTPException(404, "data-quality checks are available for Data Factory projects")
    return views.quality_view(session, access.project, access.cfg, days)


@app.get("/api/projects/{key}/quality/table")
def quality_table(access: AccessDep, session: SessionDep, name: Annotated[str, Query(min_length=3, max_length=400)]) -> dict:
    """One table's full data-quality report: thirteen checks, every column's empty share, rows, values, schema, the
    loading pipeline's runs, and the AI's explanation (aggregates only)."""
    from ..dq.service import mappings_of, shown_mappings
    from .dq_views import table_detail

    out = table_detail(session, access.project, name.upper(), shown_mappings(mappings_of(session, access.project)))
    if out is None:
        raise HTTPException(404, "no data-quality results for this table")
    return out


@app.post("/api/projects/{key}/quality/check")
def quality_check(access: AccessDep, session: SessionDep) -> dict:
    """Check every table's data now: a full bot cycle with the data checks forced (problems open incidents), then the
    AI's assessment of the aggregate results (the LangGraph workflow's analyse stage)."""
    from ..db import BotState, session_scope
    from ..dq.service import has_mappings
    from ..engine.cycle import run_cycle

    mapped = has_mappings(session, access.project.id)
    legacy = access.cfg.quality is not None and access.cfg.quality.enabled
    if access.cfg.kind != "adf" or not (mapped or legacy):
        raise HTTPException(404, "no tables are mapped for data-quality checks in this project")
    if not mapped and not access.is_owner:
        raise HTTPException(403, "only owners can run data checks on demand")
    session.commit()  # release the request's write before the cycle writes
    r = run_cycle(access.cfg, force_quality=True)  # checks, judges, and explains (AI) the mapped tables
    report = None
    with session_scope() as s:
        row = s.get(BotState, f"dq-report:{access.project.key}")
        report = row.value if row else None
    return {"status": r.status, "error": r.error, "checks": r.checks, "failed_checks": r.failed_checks,
            "opened": r.changes.opened, "resolved": r.changes.resolved, "report": report}


_REPORT_AI: dict[tuple[str, str], tuple[str, str]] = {}  # (project, date+hour) → (summary, model)


@app.get("/api/projects/{key}/report")
def morning_report(access: AccessDep, session: SessionDep, ai: bool = False) -> dict:
    """The Daily Morning Report, live. `ai=1` adds the AI summary paragraph (cached for the hour)."""
    from ..engine.report import ai_summary, build_report, report_text

    r = build_report(session, access.project, access.cfg)
    if ai:
        key = (access.project.key, r["generated_at"][:13])
        if key not in _REPORT_AI:
            session.commit()  # no transaction open while the model thinks
            if summary := ai_summary(r):
                _REPORT_AI[key] = summary
        if key in _REPORT_AI:
            r["ai_summary"], r["ai_model"] = _REPORT_AI[key]
            r["text"] = report_text(r)
    r["schedule"] = access.cfg.alerts.morning_report or None
    return r


@app.post("/api/projects/{key}/quality/{table}/accept-schema")
def quality_accept_schema(access: OwnerDep, session: SessionDep, table: str) -> dict:
    """Owner action: a table's changed columns become its accepted schema (the incident resolves on the next check)."""
    from ..dq.service import has_mappings
    from ..engine.quality import accept_schema as do_accept

    if access.cfg.kind != "adf" or (access.cfg.quality is None and not has_mappings(session, access.project.id)):
        raise HTTPException(404, "no data-quality checks are configured for this project")
    if not do_accept(views._ctx(session, access.project, access.cfg, utcnow()), table.upper()):
        raise HTTPException(404, "no schema change recorded for this table")
    return {"accepted": table.upper()}


@app.post("/api/projects/{key}/report/send")
def morning_report_send(access: AccessDep, session: SessionDep) -> dict:
    """Send today's report to the project's Teams channel now (owners)."""
    from ..engine.report import send_report

    if not access.is_owner:
        raise HTTPException(403, "only owners can send the report")
    session.commit()
    r = send_report(access.cfg, force=True)
    return {"sent": r is not None, "delivery": (r or {}).get("delivery")}


@app.get("/api/projects/{key}/loads")
def loads(access: AccessDep, session: SessionDep, days: Annotated[int, Query(ge=1, le=120)] = 30) -> dict:
    """Data Factory projects: every table the pipelines load, with row counts per load and what's normal for it."""
    if access.cfg.kind != "adf":
        raise HTTPException(404, "table loads are available for Data Factory projects")
    return views.loads_view(session, access.project, access.cfg, days)


@app.get("/api/projects/{key}/runs/{run_id}/tree")
def run_tree(access: AccessDep, session: SessionDep, run_id: str) -> dict:
    """ADF projects: the run's failure tree, i.e. every authored step with what actually ran, read live from the factory."""
    from ..connectors.adf import failure_path
    from ..db import Run

    if access.cfg.kind != "adf":
        raise HTTPException(404, "failure trees are available for Data Factory projects")
    run = session.scalars(select(Run).where(Run.project_id == access.project.id, Run.run_id == run_id)).first()
    if run is None:
        raise HTTPException(404, "run not found")
    adf = views._ctx(session, access.project, access.cfg, utcnow()).connectors.pipeline
    try:
        steps = adf.run_tree(run.site, run_id)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - shown to the user, who can't act on a stack trace anyway
        raise HTTPException(502, f"Couldn't read the run from Azure Data Factory: {exc}") from exc
    # what each target table got from this run (and which ones it didn't load)
    try:
        targets = adf.step_targets(run.site)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        targets = {}
    tables = []

    def walk(nodes: list[dict]) -> None:
        for n in nodes:
            if n["type"] == "Copy":
                t = targets.get(n["name"], {})
                copy = next((r["copy"] for r in reversed(n["runs"]) if r.get("copy")), None) or {}
                tables.append({"step": n["name"], "table": t.get("table") or n["name"], "env": t.get("env"), "kind": t.get("kind"),
                               "status": n["status"], "rows_read": copy.get("rows_read"), "rows_copied": copy.get("rows_copied"),
                               "rows_skipped": copy.get("rows_skipped"),
                               "error": next((r["error"] for r in n["runs"] if r.get("error")), None)})
            walk(n["children"])

    walk(steps)
    return {"pipeline": run.site, "run_id": run_id, "status": run.run_status,
            "monitor_url": adf.monitor_url(run_id),  # type: ignore[attr-defined]
            "steps": steps, "failure_path": failure_path(steps), "tables": tables}


@app.get("/api/projects/{key}/runs")
def runs(
    access: AccessDep, session: SessionDep,
    site: str | None = None,
    status: Literal["all", "failed", "running"] = "all",
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict:
    return {**views.runs_list(session, access.project, site, None if status == "all" else status, limit, access.cfg), "kind": access.cfg.kind}


@app.get("/api/projects/{key}/series")
def series(
    access: AccessDep, session: SessionDep, site: str,
    metric: Literal["row_count", "null_rate", "duplicate_rate", "duration_s"] = "row_count",
    field: str = "", output: str | None = None,
    limit: Annotated[int, Query(ge=2, le=200)] = 40,
) -> dict:
    return views.metric_series(session, access.project, access.cfg, site, metric, field, output, limit)


@app.get("/api/projects/{key}/tables")
def tables(access: AccessDep, session: SessionDep) -> list[dict]:
    return views.tables_view(session, access.project, access.cfg)


@app.get("/api/projects/{key}/alerts")
def alerts(access: AccessDep, session: SessionDep, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict:
    return {"notifier": views.notifier_info(access.cfg), "alerts": views.alerts_list(session, access.project, limit)}


@app.get("/api/projects/{key}/config")
def config(access: AccessDep) -> dict:
    return {**views.config_view(access.cfg), "can_edit": access.is_owner}


# ── owner actions ─────────────────────────────────────────────────────────────


class MuteIn(BaseModel):
    scope: str = Field(default="*", max_length=256, description="'*' for the whole project, or an incident scope")
    hours: float = Field(default=4, gt=0, le=24 * 14)
    reason: str = Field(default="", max_length=500)


@app.get("/api/projects/{key}/mutes")
def mutes(access: AccessDep, session: SessionDep) -> list[dict]:
    return views.mutes_list(session, access.project)


@app.post("/api/projects/{key}/mutes", status_code=201)
def add_mute(access: OwnerDep, session: SessionDep, body: MuteIn) -> list[dict]:
    session.add(Mute(project_id=access.project.id, scope=body.scope, reason=body.reason,
                     until=utcnow() + timedelta(hours=body.hours), created_by=access.user.name))
    session.flush()
    return views.mutes_list(session, access.project)


@app.delete("/api/projects/{key}/mutes/{mute_id}")
def end_mute(access: OwnerDep, session: SessionDep, mute_id: int) -> list[dict]:
    m = session.get(Mute, mute_id)
    if m is None or m.project_id != access.project.id:
        raise HTTPException(404, "mute not found")
    m.until = utcnow()
    session.flush()
    return views.mutes_list(session, access.project)


@app.post("/api/projects/{key}/tables/{output_id}/accept-schema")
def accept_schema(access: OwnerDep, session: SessionDep, output_id: str) -> list[dict]:
    from ..engine.checks import accept_schema as do_accept

    if output_id not in access.cfg.schema_tables:
        raise HTTPException(404, "output not found")
    ctx = views._ctx(session, access.project, access.cfg, utcnow())
    do_accept(ctx, output_id, utcnow())
    session.flush()
    return views.tables_view(session, access.project, access.cfg)


# ── portal (production build) ─────────────────────────────────────────────────

_DIST = Path(__file__).resolve().parents[3] / "portal" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404)
        file = _DIST / path
        return FileResponse(file if path and file.is_file() else _DIST / "index.html")
