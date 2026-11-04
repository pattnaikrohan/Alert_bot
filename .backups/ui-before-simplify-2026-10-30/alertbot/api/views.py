"""Read models for the portal. Pure functions: session in, JSON-ready dicts out.

Datetimes are always emitted as ISO-8601 UTC with a trailing `Z` (via `iso`),
so the browser can render them in the viewer's own timezone.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import FEED_OUTPUT_ID, ProjectConfig, load_ai_config, load_projects
from ..db import (
    AgentToolCall,
    BotCycle,
    BotState,
    CheckResult,
    DqMapping,
    Incident,
    IncidentEvent,
    MetricObservation,
    Mute,
    Notification,
    Project,
    Run,
    SchemaSnapshot,
)
from ..engine.baselines import band_for
from ..engine.checks import SEVERITY_RANK
from ..engine.context import ProjectContext
from ..secrets import resolve_secret
from ..settings import get_settings
from ..timeutil import WEEKDAYS, iso, local_weekday, utcnow

ACTIVE = ("open", "acknowledged")


# ── shared bits ───────────────────────────────────────────────────────────────


def last_ok_cycle(session: Session, project_id: int) -> BotCycle | None:
    return session.scalars(
        select(BotCycle).where(BotCycle.project_id == project_id, BotCycle.status == "ok")
        .order_by(BotCycle.started_at.desc(), BotCycle.id.desc()).limit(1)
    ).first()


def last_cycle(session: Session, project_id: int) -> BotCycle | None:
    return session.scalars(
        select(BotCycle).where(BotCycle.project_id == project_id).order_by(BotCycle.started_at.desc(), BotCycle.id.desc()).limit(1)
    ).first()


def health_of(cycle: BotCycle | None) -> float | None:
    if cycle is None or not cycle.checks_run:
        return None
    return round(100 * (cycle.checks_run - cycle.checks_failed) / cycle.checks_run, 1)


def project_status(session: Session, project_id: int) -> str:
    sev = session.scalars(
        select(Incident.severity).where(Incident.project_id == project_id, Incident.status.in_(ACTIVE))
    ).all()
    if not sev:
        return "ok"
    return max(sev, key=lambda s: SEVERITY_RANK[s])


def _today_start_utc() -> datetime:
    """Midnight today in the business timezone, as naive UTC."""
    tz = get_settings().tz
    midnight = datetime.combine(datetime.now(tz).date(), time(), tzinfo=tz)
    return midnight.astimezone(UTC).replace(tzinfo=None)


def incident_brief(i: Incident) -> dict[str, Any]:
    return {
        "id": i.id, "title": i.title, "severity": i.severity, "status": i.status, "scope": i.scope,
        "scope_label": i.scope.split(":", 1)[-1], "checks": i.checks, "likely_cause": i.likely_cause,
        "summary": i.summary, "analysis_source": i.analysis_source, "confidence": i.confidence,
        "opened_at": iso(i.opened_at), "updated_at": iso(i.updated_at), "last_seen_at": iso(i.last_seen_at),
        "acknowledged_at": iso(i.acknowledged_at), "acknowledged_by": i.acknowledged_by,
        "resolved_at": iso(i.resolved_at), **_resolver(i),
    }


def _resolver(i: Incident) -> dict[str, Any]:
    """Who resolved it and how. Incidents resolved before this was recorded: only the bot resolved them then, and its
    "resolved" event says why."""
    if i.status != "resolved" or i.resolved_by:
        return {"resolved_by": i.resolved_by, "resolution": i.resolution}
    why = next((e.message for e in reversed(i.events) if e.kind == "resolved"), None)
    return {"resolved_by": "Alert Bot", "resolution": i.resolution or why}


def comments_of(session: Session, ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """People's comments on incidents, oldest first."""
    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for e in session.scalars(select(IncidentEvent).where(IncidentEvent.incident_id.in_(ids or [-1]), IncidentEvent.kind == "comment")
                             .order_by(IncidentEvent.at, IncidentEvent.id)):
        out[e.incident_id].append({"id": e.id, "at": iso(e.at), "by": (e.data or {}).get("by") or "Someone", "text": e.message})
    return out


def notifier_info(cfg: ProjectConfig) -> dict[str, Any]:
    configured = bool(resolve_secret(cfg.alerts.flow_url)) if cfg.alerts.notifier != "none" else False
    return {
        "channel": cfg.alerts.notifier,
        "mode": "live" if configured else ("off" if cfg.alerts.notifier == "none" else "dry_run"),
        "teams_channel": cfg.alerts.teams_channel,
        "flow_url_ref": cfg.alerts.flow_url or None,  # the reference (env://, kv://), never the URL
        "email_owners_on": cfg.alerts.email_owners_on,
    }


def ai_info() -> dict[str, Any]:
    ai = load_ai_config()
    return {"enabled": ai.ready, "provider": ai.provider or None, "model": (ai.chat_models or [None])[0],
            "models": ai.chat_models, "framework": ai.provider or "rules"}


# ── projects list (for /me) ───────────────────────────────────────────────────


def project_card(session: Session, project: Project, role: str) -> dict[str, Any]:
    cycle = last_ok_cycle(session, project.id)
    by_status = dict(session.execute(
        select(Incident.status, func.count()).where(Incident.project_id == project.id, Incident.status.in_(ACTIVE)).group_by(Incident.status)
    ).all())
    open_n = sum(by_status.values())
    attention = session.scalars(select(Incident.severity).where(Incident.project_id == project.id, Incident.status == "open")).all()
    resolved_today = session.scalar(select(func.count()).select_from(Incident).where(
        Incident.project_id == project.id, Incident.status == "resolved", Incident.resolved_at >= _today_start_utc()))
    return {
        "key": project.key, "display_name": project.display_name, "description": project.description, "role": role,
        "kind": cfg.kind if (cfg := load_projects().get(project.key)) else None,
        "service": ({"adf": "Data Factory", "synapse": "Synapse", "logic_apps": "Logic Apps"}.get(cfg.pipeline.type)
                    if cfg and cfg.kind == "adf" and cfg.pipeline else None),
        "status": project_status(session, project.id), "open_incidents": open_n, "health": health_of(cycle),
        # not yet acknowledged by anyone: what the badges count (acknowledged ones are being handled, still unresolved)
        "needs_attention": len(attention), "acknowledged": by_status.get("acknowledged", 0), "resolved_today": resolved_today or 0,
        "attention_severity": max(attention, key=lambda s: SEVERITY_RANK[s]) if attention else None,
        "last_cycle_at": iso(cycle.started_at) if cycle else None,
    }


# ── overview ──────────────────────────────────────────────────────────────────


def overview(session: Session, project: Project, cfg: ProjectConfig, role: str) -> dict[str, Any]:
    now = utcnow()
    cycle = last_ok_cycle(session, project.id)
    any_cycle = last_cycle(session, project.id)
    today = _today_start_utc()

    runs_today = session.scalars(select(Run).where(Run.project_id == project.id, Run.requested_at >= today)).all()
    active = session.scalars(
        select(Incident).where(Incident.project_id == project.id, Incident.status.in_(ACTIVE))
        .order_by(Incident.opened_at.desc())
    ).all()

    # Per-site status from the latest cycle's check results.
    results = session.scalars(select(CheckResult).where(CheckResult.cycle_id == cycle.id)).all() if cycle else []
    by_scope: dict[str, list[CheckResult]] = defaultdict(list)
    for r in results:
        by_scope[r.scope].append(r)
    incident_by_scope = {i.scope: i for i in active}

    latest_run: dict[str, Run] = {}
    for r in session.scalars(
        select(Run).where(Run.project_id == project.id, Run.requested_at >= now - timedelta(days=14))
        .order_by(Run.requested_at.desc())
    ):
        latest_run.setdefault(r.site, r)

    ctx = _ctx(session, project, cfg, now)
    sites = []
    for site in sorted(latest_run):
        scope = f"site:{site}"
        rs = by_scope.get(scope, [])
        failing = [r for r in rs if r.status == "fail"]
        status = max((r.severity or "warning" for r in failing), key=lambda s: SEVERITY_RANK[s]) if failing else ("ok" if rs else "unknown")
        run = latest_run[site]
        band = band_for(ctx, cfg.primary_output_id, site, "row_count", "", local_weekday(now, get_settings().tz), before=now)
        last_rows = session.scalar(
            select(MetricObservation.value).where(
                MetricObservation.project_id == project.id, MetricObservation.site == site,
                MetricObservation.metric == "row_count", MetricObservation.output_id == cfg.primary_output_id)
            .order_by(MetricObservation.observed_at.desc()).limit(1)
        )
        spark = list(reversed(session.scalars(
            select(MetricObservation.value).where(
                MetricObservation.project_id == project.id, MetricObservation.site == site,
                MetricObservation.metric == "row_count", MetricObservation.output_id == cfg.primary_output_id)
            .order_by(MetricObservation.observed_at.desc()).limit(21)
        ).all()))
        inc = incident_by_scope.get(scope)
        sites.append({
            "site": site, "status": status,
            "failing": [{"check": r.check, "severity": r.severity, "message": r.message} for r in failing],
            "last_run": _run_dict(run),
            "last_rows": last_rows,
            "expected": {"low": band.low, "high": band.high, "median": band.median, "basis": band.basis} if band else None,
            "sparkline": spark,
            "incident_id": inc.id if inc else None,
        })

    output_scopes = [s for s in by_scope if s.startswith("output:")]
    tables = []
    for scope in output_scopes:
        failing = [r for r in by_scope[scope] if r.status == "fail"]
        tables.append({"output": scope.split(":", 1)[1], "status": failing[0].severity if failing else "ok",
                       "message": failing[0].message if failing else ""})

    events = session.execute(
        select(IncidentEvent, Incident.title, Incident.severity).join(Incident, Incident.id == IncidentEvent.incident_id)
        .where(Incident.project_id == project.id, IncidentEvent.kind.in_(("opened", "worsened", "resolved", "acknowledged")))
        .order_by(IncidentEvent.at.desc(), IncidentEvent.id.desc()).limit(12)
    ).all()

    # 14-day activity for the overview chart.
    since = today - timedelta(days=13)
    daily: dict[str, dict[str, int]] = {}
    for d in range(14):
        day = (since + timedelta(days=d)).date().isoformat()
        daily[day] = {"runs": 0, "failed": 0, "rows": 0}
    for r in session.scalars(select(Run).where(Run.project_id == project.id, Run.requested_at >= since)):
        day = r.requested_at.date().isoformat()
        if day in daily:
            daily[day]["runs"] += 1
            daily[day]["failed"] += int(r.run_status in ("Failed", "Cancelled"))
            daily[day]["rows"] += r.rows_loaded or 0

    return {
        "project": {"key": project.key, "display_name": project.display_name, "description": project.description, "role": role},
        "status": project_status(session, project.id),
        "health": health_of(cycle),
        "kpis": {
            "runs_today": len(runs_today),
            "failed_today": sum(r.run_status in ("Failed", "Cancelled") for r in runs_today),
            "rows_today": sum(r.rows_loaded or 0 for r in runs_today),
            "open_incidents": len(active),
            "critical_open": sum(i.severity == "critical" for i in active),
            "checks": cycle.checks_run if cycle else 0,
            "checks_failing": cycle.checks_failed if cycle else 0,
        },
        "bot": {
            "last_cycle_at": iso(any_cycle.started_at) if any_cycle else None,
            "last_cycle_status": any_cycle.status if any_cycle else None,
            "last_error": any_cycle.error if any_cycle and any_cycle.status == "error" else None,
            "schedule": cfg.schedule,
        },
        "sites": sites,
        "tables": tables,
        "active_incidents": [incident_brief(i) for i in active],
        "recent_events": [
            {"at": iso(e.at), "kind": e.kind, "message": e.message, "incident_id": e.incident_id, "title": t, "severity": sev}
            for e, t, sev in events
        ],
        "activity": [{"date": k, **v} for k, v in daily.items()],
        "notifier": notifier_info(cfg),
        "ai": ai_info(),
    }


def _ctx(session: Session, project: Project, cfg: ProjectConfig, now: datetime) -> ProjectContext:
    from ..engine.cycle import _connectors

    return ProjectContext(cfg=cfg, project=project, session=session, connectors=_connectors(cfg),
                          settings=get_settings(), now=now)


def _run_dict(r: Run) -> dict[str, Any]:
    return {
        "id": r.id, "trigger_id": r.trigger_id, "site": r.site, "trigger_status": r.trigger_status,
        "run_id": r.run_id, "pipeline": r.pipeline, "run_status": r.run_status,
        "requested_at": iso(r.requested_at), "started_at": iso(r.started_at), "ended_at": iso(r.ended_at),
        "duration_s": r.duration_s, "rows_loaded": r.rows_loaded, "error": r.error,
    }


# ── incidents ─────────────────────────────────────────────────────────────────


def incidents_list(session: Session, project: Project, status: str, limit: int) -> list[dict[str, Any]]:
    q = select(Incident).where(Incident.project_id == project.id)
    if status == "active":
        q = q.where(Incident.status.in_(ACTIVE))
    elif status == "resolved":
        q = q.where(Incident.status == "resolved")
    rows = list(session.scalars(q.order_by(Incident.opened_at.desc()).limit(limit)))
    notes = comments_of(session, [i.id for i in rows])
    return [{**incident_brief(i), "comments": len(notes.get(i.id, [])), "last_comment": (notes.get(i.id) or [None])[-1]} for i in rows]


def incident_detail(session: Session, project: Project, cfg: ProjectConfig, incident: Incident) -> dict[str, Any]:
    checks = session.scalars(
        select(CheckResult).where(CheckResult.incident_id == incident.id).order_by(CheckResult.created_at.desc())
    ).all()
    latest: dict[str, CheckResult] = {}
    for c in checks:
        latest.setdefault(c.check, c)
    tool_calls = session.scalars(
        select(AgentToolCall).where(AgentToolCall.incident_id == incident.id).order_by(AgentToolCall.at.desc(), AgentToolCall.id.desc()).limit(40)
    ).all()
    notes = session.scalars(
        select(Notification).where(Notification.incident_id == incident.id).order_by(Notification.created_at.desc())
    ).all()
    run = None
    if incident.run_ref:
        r = session.scalars(select(Run).where(Run.project_id == project.id, Run.run_id == incident.run_ref)).first()
        run = _run_dict(r) if r else None
        if r is not None and (norms := _norms(session, project, cfg)):
            run["vs_normal"] = norms.compare(r.site, r.duration_s, status=r.run_status, started_at=r.started_at)

    site = incident.scope.split(":", 1)[1] if incident.scope.startswith("site:") else None
    charts = []
    if site:
        names = {c.check for c in checks}
        if names & {"row_count", "empty_site"}:
            charts.append({"metric": "row_count", "field": "", "label": "Rows per run"})
        for c in latest.values():
            if c.check == "null_spike":
                for f in c.details.get("fields", []):
                    charts.append({"metric": "null_rate", "field": f["field"], "label": f"Empty {f['field']} (share of rows)"})
        if "run_too_slow" in names:
            charts.append({"metric": "duration_s", "field": "", "label": "Run duration", "output": "_pipeline"})

    fix = None
    if cfg.kind == "adf" and incident.status != "resolved":
        from ..ai.remediate import remediation

        adf = _ctx(session, project, cfg, utcnow()).connectors.pipeline
        run_id = next((c.details.get("run_id") for c in latest.values() if c.details.get("run_id")), None) or incident.run_ref
        pipeline = site or next((c.details.get("pipeline") for c in latest.values() if c.details.get("pipeline")), None)
        factory = getattr(adf, "factory_id", None)
        fix = remediation(incident, latest, teams_channel=cfg.alerts.teams_channel,
                          monitor_url=adf.monitor_url(run_id) if run_id and run_id.count("-") == 4 and factory else None,  # type: ignore[attr-defined]
                          studio_url=f"https://adf.azure.com/en/authoring/pipeline/{pipeline}?factory={factory}" if pipeline and factory else None)

    return {
        **incident_brief(incident),
        "impact": incident.impact,
        "suggested_action": incident.suggested_action,
        "evidence": incident.evidence,
        "last_seen_at": iso(incident.last_seen_at),
        "acknowledged_at": iso(incident.acknowledged_at),
        "site": site,
        "run": run,
        "kind": cfg.kind,  # adf: the portal offers the run's failure tree
        "failing_checks": [
            {"check": c.check, "severity": c.severity, "message": c.message, "actual": c.actual,
             "expected_low": c.expected_low, "expected_high": c.expected_high, "at": iso(c.created_at), "details": c.details}
            for c in sorted(latest.values(), key=lambda c: -SEVERITY_RANK.get(c.severity or "info", 0))
        ],
        "events": [{"at": iso(e.at), "kind": e.kind, "message": e.message, "data": e.data} for e in reversed(incident.events) if e.kind != "comment"],
        "comments": comments_of(session, [incident.id]).get(incident.id, []),
        "tool_calls": [
            {"at": iso(t.at), "agent": t.agent, "tool": t.tool, "args": t.args, "result": t.result,
             "duration_ms": t.duration_ms, "error": t.error}
            for t in tool_calls
        ],
        "notifications": [_note_dict(n) for n in notes],
        "charts": charts,
        "remediation": fix,
    }


def _note_dict(n: Notification) -> dict[str, Any]:
    return {"id": n.id, "event": n.event, "status": n.status, "channel": n.channel, "attempts": n.attempts,
            "last_error": n.last_error, "created_at": iso(n.created_at), "sent_at": iso(n.sent_at),
            "incident_id": n.incident_id, "title": n.payload.get("title"), "severity": n.payload.get("severity"),
            "payload": n.payload}


def alerts_list(session: Session, project: Project, limit: int) -> list[dict[str, Any]]:
    return [_note_dict(n) for n in session.scalars(
        select(Notification).where(Notification.project_id == project.id).order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    )]


# ── runs, metrics, tables ─────────────────────────────────────────────────────


def runs_list(session: Session, project: Project, site: str | None, status: str | None, limit: int,
              cfg: ProjectConfig | None = None) -> dict[str, Any]:
    q = select(Run).where(Run.project_id == project.id)
    if site:
        q = q.where(Run.site == site)
    if status == "failed":
        q = q.where(Run.run_status.in_(("Failed", "Cancelled")))
    elif status == "running":
        q = q.where(Run.run_status.in_(("InProgress", "Queued")) | (Run.trigger_status == "pending"))
    rows = session.scalars(q.order_by(Run.requested_at.desc()).limit(limit)).all()
    sites = session.scalars(select(Run.site).where(Run.project_id == project.id).distinct().order_by(Run.site)).all()
    norms = _norms(session, project, cfg)
    return {"runs": [{**_run_dict(r), "vs_normal": norms.compare(r.site, r.duration_s, status=r.run_status, started_at=r.started_at) if norms else None}
                     for r in rows], "sites": sites}


def _norms(session: Session, project: Project, cfg: ProjectConfig | None) -> Any:
    """Normal run times for the project's pipelines (ADF / Synapse / Logic Apps), or None."""
    if cfg is None or cfg.kind != "adf":
        return None
    from ..engine.runtime import Norms

    return Norms(_ctx(session, project, cfg, utcnow()))


def metric_series(session: Session, project: Project, cfg: ProjectConfig, site: str, metric: str, field: str,
                  output: str | None, limit: int) -> dict[str, Any]:
    output_id = output or cfg.primary_output_id
    obs = list(reversed(session.scalars(
        select(MetricObservation).where(
            MetricObservation.project_id == project.id, MetricObservation.output_id == output_id,
            MetricObservation.site == site, MetricObservation.metric == metric, MetricObservation.field == field)
        .order_by(MetricObservation.observed_at.desc()).limit(limit)
    ).all()))
    ctx = _ctx(session, project, cfg, utcnow())
    points = []
    for o in obs:
        b = band_for(ctx, output_id, site, metric, field, o.weekday, before=o.observed_at, exclude_run=o.run_ref)
        points.append({"t": iso(o.observed_at), "value": o.value, "run": o.run_ref, "weekday": WEEKDAYS[o.weekday],
                       "low": b.low if b else None, "high": b.high if b else None, "median": b.median if b else None})
    return {"site": site, "metric": metric, "field": field, "output": output_id, "points": points}


def tables_view(session: Session, project: Project, cfg: ProjectConfig) -> list[dict[str, Any]]:
    from dataclasses import asdict

    from ..engine.checks import diff_schema

    ctx = _ctx(session, project, cfg, utcnow())
    feeds = cfg.kind == "feeds"
    # Feeds: one entry per distinct table; its metrics live under the shared "feed" output, one site per feed.
    entries = (
        [SimpleNamespace(id=tid, source=src, table=table, key=next(f.key for f in cfg.feeds if f.table == table),
                         group_by="feed", loaded_at=next(f.timestamp for f in cfg.feeds if f.table == table),
                         required_fields=sorted({r for f in cfg.feeds if f.table == table for r in f.required_fields}),
                         sites={f.name for f in cfg.feeds if f.table == table})
         for tid, (src, table) in cfg.schema_tables.items()]
        if feeds else list(cfg.outputs)
    )
    out = []
    for o in entries:
        snap = session.scalars(
            select(SchemaSnapshot).where(SchemaSnapshot.project_id == project.id, SchemaSnapshot.output_id == o.id)
            .order_by(SchemaSnapshot.captured_at.desc(), SchemaSnapshot.id.desc()).limit(1)
        ).first()
        try:
            live = [asdict(c) for c in ctx.connectors.source(o.source).read_schema(o.table if feeds else o)]
            live_error = None
        except Exception as exc:  # noqa: BLE001
            live, live_error = [], str(exc)
        diff = diff_schema(snap.columns, live) if snap and live else {"added": [], "removed": [], "retyped": []}

        latest: dict[tuple[str, str, str], MetricObservation] = {}
        out_id = FEED_OUTPUT_ID if feeds else o.id
        last_at = session.scalar(select(func.max(MetricObservation.observed_at)).where(
            MetricObservation.project_id == project.id, MetricObservation.output_id == out_id)) or utcnow()
        for m in session.scalars(  # the 14 days up to its latest measurement: a table that went quiet still shows them
            select(MetricObservation).where(MetricObservation.project_id == project.id, MetricObservation.output_id == out_id,
                                            MetricObservation.observed_at >= last_at - timedelta(days=14))
            .order_by(MetricObservation.observed_at.desc())
        ):
            latest.setdefault((m.site, m.metric, m.field), m)
        sites: dict[str, dict[str, Any]] = {}
        for (site, metric, field), m in latest.items():
            if feeds and site not in o.sites:
                continue
            s = sites.setdefault(site, {"site": site, "rows": None, "null_rates": {}, "duplicate_rate": None, "at": None})
            if metric == "row_count":
                s["rows"], s["at"] = m.value, iso(m.observed_at)
            elif metric == "null_rate":
                s["null_rates"][field] = m.value
            elif metric == "duplicate_rate":
                s["duplicate_rate"] = m.value
        out.append({
            "id": o.id, "table": o.table, "key": o.key, "group_by": o.group_by, "loaded_at": o.loaded_at,
            "required_fields": o.required_fields,
            "schema": {"accepted": snap.columns if snap else None, "accepted_at": iso(snap.captured_at) if snap else None,
                       "live": live, "live_error": live_error, "diff": diff,
                       "drifted": bool(diff["added"] or diff["removed"] or diff["retyped"])},
            "sites": sorted(sites.values(), key=lambda s: s["site"]),
        })
    return out


def loads_view(session: Session, project: Project, cfg: ProjectConfig, days: int = 30) -> dict[str, Any]:
    """ADF projects: exactly how many rows each table received, per load and per day, in a period of whole local days
    (days=1 is today; 7 is today and the 6 days before), against the period before; plus the latest load, the learned
    normal, data-quality status and the open incident. Tables that got nothing in the period are listed too."""
    from ..engine.adf import TABLE_PREFIX

    now = utcnow()
    tz = get_settings().tz
    ctx = _ctx(session, project, cfg, now)
    today = now.replace(tzinfo=UTC).astimezone(tz).date()
    first_day = today - timedelta(days=days - 1)
    start = datetime.combine(first_day, time.min, tzinfo=tz).astimezone(UTC).replace(tzinfo=None)
    prev_start = start - timedelta(days=days)
    day_of = lambda dt: dt.replace(tzinfo=UTC).astimezone(tz).date()  # noqa: E731

    obs = session.scalars(
        select(MetricObservation).where(MetricObservation.project_id == project.id, MetricObservation.output_id.like(f"{TABLE_PREFIX}%"),
                                        MetricObservation.observed_at >= prev_start).order_by(MetricObservation.observed_at))
    loads: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for o in obs:
        loads[(o.output_id, o.site)].setdefault(o.run_ref, {"at": o.observed_at, "run_id": o.run_ref, "weekday": o.weekday})[o.metric] = o.value
    # tables seen before the window still belong in the list (with nothing inserted in it)
    for output_id, site, at in session.execute(
            select(MetricObservation.output_id, MetricObservation.site, func.max(MetricObservation.observed_at))
            .where(MetricObservation.project_id == project.id, MetricObservation.output_id.like(f"{TABLE_PREFIX}%"),
                   MetricObservation.observed_at < prev_start)
            .group_by(MetricObservation.output_id, MetricObservation.site)):
        if (output_id, site) not in loads:
            older = session.scalars(select(MetricObservation).where(
                MetricObservation.project_id == project.id, MetricObservation.output_id == output_id, MetricObservation.site == site,
                MetricObservation.observed_at == at)).all()
            for o in older:
                loads[(output_id, site)].setdefault(o.run_ref, {"at": o.observed_at, "run_id": o.run_ref, "weekday": o.weekday})[o.metric] = o.value
    run_ids = {h["run_id"] for per in loads.values() for h in per.values()}
    runs = {r.run_id: r for r in session.scalars(select(Run).where(Run.project_id == project.id, Run.run_id.in_(run_ids or [""])))}
    active = {i.scope: i for i in session.scalars(select(Incident).where(Incident.project_id == project.id, Incident.status.in_(ACTIVE)))}
    dq: dict[str, dict[str, Any]] = {}
    if cfg.quality is not None and cfg.quality.enabled:
        dq = {t["table"].upper(): t for t in quality_view(session, project, cfg, days=max(days, 7))["tables"]}
    steps_of: dict[str, dict[str, Any]] = {}
    calendar = [first_day + timedelta(days=i) for i in range(days)]
    totals_by_day: dict[Any, dict[str, Any]] = {d: {"date": d.isoformat(), "rows": 0, "loads": 0, "tables": {}} for d in calendar}
    out = []
    for (output_id, pipeline), per_run in loads.items():
        table = output_id.removeprefix(TABLE_PREFIX)
        if pipeline not in steps_of:
            try:
                steps_of[pipeline] = ctx.connectors.pipeline.step_targets(pipeline)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - names and environments are a bonus
                steps_of[pipeline] = {}
        meta = next((t for t in steps_of[pipeline].values() if t["table"] == table), {})
        hist = sorted(per_run.values(), key=lambda h: h["at"])
        cur = [h for h in hist if h["at"] >= start]
        prev = [h for h in hist if prev_start <= h["at"] < start]
        last = hist[-1]
        band = band_for(ctx, output_id, pipeline, "row_count", "", last["weekday"], before=last["at"] + timedelta(seconds=1),
                        exclude_run=last["run_id"])
        daily = {d: 0.0 for d in calendar}
        for h in cur:
            d = day_of(h["at"])
            if d in daily:
                daily[d] += h.get("row_count") or 0
                tot = totals_by_day[d]
                tot["rows"] += h.get("row_count") or 0
                tot["loads"] += 1
                tot["tables"][table] = tot["tables"].get(table, 0) + (h.get("row_count") or 0)
        inc = active.get(f"table:{table}") or active.get(f"data:{table}")
        q = dq.get(table.upper())

        def load(h: dict[str, Any]) -> dict[str, Any]:
            r = runs.get(h["run_id"])
            return {"at": iso(h["at"]), "run_id": h["run_id"], "run_status": r.run_status if r else None,
                    "duration_s": r.duration_s if r else None, "trigger": (r.pipeline or "").split(" · ")[-1] if r and r.pipeline else None,
                    "rows_copied": h.get("row_count"), "rows_read": h.get("rows_read"), "rows_skipped": h.get("rows_skipped")}

        inserted = sum(h.get("row_count") or 0 for h in cur)
        out.append({
            "table": table, "pipeline": pipeline, "kind": meta.get("kind", "table"), "env": meta.get("env"),
            "dataset": meta.get("dataset"), "steps": [s for s, t in steps_of[pipeline].items() if t["table"] == table],
            "last": load(last),
            "normal": {"median": band.median, "low": band.low, "high": band.high, "samples": band.samples} if band else None,
            "inserted": inserted, "inserted_prev": sum(h.get("row_count") or 0 for h in prev) if prev else None,
            "read": sum(h.get("rows_read") or 0 for h in cur), "skipped": sum(h.get("rows_skipped") or 0 for h in cur),
            "loads_count": len(cur), "zero_loads": sum(1 for h in cur if not h.get("row_count")),
            "skipped_total": sum(h.get("rows_skipped") or 0 for h in cur),
            "daily": [{"date": d.isoformat(), "rows": v} for d, v in daily.items()],
            "loads": [load(h) for h in reversed(cur)][:500],
            "status": inc.severity if inc else "ok", "incident": {"id": inc.id, "title": inc.title} if inc else None,
            "quality": {"status": q["status"], "score": q["score"], "failing": [c["label"] for c in q["checks"] if c["status"] == "fail"],
                        "checked_at": q["checked_at"]} if q else None,
        })
    env_rank = {"PROD": 0, "UAT": 1, "DEV": 2}
    out.sort(key=lambda t: (t["pipeline"], t["table"].split(".")[-1], env_rank.get(t["env"] or "", 3)))
    loaded = [t for t in out if t["loads_count"]]
    with_prev = [t["inserted_prev"] for t in out if t["inserted_prev"] is not None]
    return {
        "tables": out, "days": days, "from": first_day.isoformat(), "to": today.isoformat(), "timezone": str(tz),
        "totals": {
            "inserted": sum(t["inserted"] for t in out), "inserted_prev": sum(with_prev) if with_prev else None,
            "loads": sum(t["loads_count"] for t in out), "tables": len(out), "tables_loaded": len(loaded),
            "skipped": sum(t["skipped"] for t in out), "needs_attention": sum(t["status"] != "ok" or bool(t["quality"] and t["quality"]["status"] == "fail") for t in out),
            "empty_loads": sum(t["zero_loads"] for t in out),
        },
        "daily": [{"date": v["date"], "rows": v["rows"], "loads": v["loads"],
                   "top": [{"table": k, "rows": n} for k, n in sorted(v["tables"].items(), key=lambda kv: -kv[1])[:4]]}
                  for v in totals_by_day.values()],
    }


def management_view(session: Session, project: Project, cfg: ProjectConfig, days: int = 30) -> dict[str, Any]:
    """Everything management checks, for one project and period: is data arriving, how reliable the pipelines are,
    what's broken and since when, which tables are behind, and whether it's getting better or worse."""
    import statistics
    from collections import Counter

    from ..ai.adf_explain import kind_of

    now = utcnow()
    start, prev_start = now - timedelta(days=days), now - timedelta(days=days * 2)
    runs = list(session.scalars(select(Run).where(Run.project_id == project.id, Run.requested_at >= prev_start)))
    cur = [r for r in runs if r.requested_at >= start]
    prev = [r for r in runs if r.requested_at < start]

    def finished(rs: list[Run]) -> list[Run]:
        return [r for r in rs if r.run_status in ("Succeeded", "Failed", "Cancelled")]

    def rate(rs: list[Run]) -> float | None:
        f = finished(rs)
        return round(100 * sum(r.run_status == "Succeeded" for r in f) / len(f), 1) if f else None

    # ── per day: runs by outcome, rows delivered ──
    tz = get_settings().tz
    day_of = lambda dt: dt.replace(tzinfo=UTC).astimezone(tz).date()  # noqa: E731
    daily: dict[Any, dict[str, Any]] = {}
    for i in range(days):
        d = (now - timedelta(days=days - 1 - i)).replace(tzinfo=UTC).astimezone(tz).date()
        daily[d] = {"date": d.isoformat(), "succeeded": 0, "failed": 0, "rows": 0}
    for r in finished(cur):
        if (d := day_of(r.requested_at)) in daily:
            daily[d]["succeeded" if r.run_status == "Succeeded" else "failed"] += 1

    # rows: tables for ADF projects (PROD only when environments exist), else the primary output
    adf = cfg.kind == "adf"
    rows_q = select(MetricObservation).where(MetricObservation.project_id == project.id, MetricObservation.metric == "row_count",
                                             MetricObservation.observed_at >= prev_start)
    rows_q = rows_q.where(MetricObservation.output_id.like("table:%")) if adf else rows_q.where(MetricObservation.output_id == cfg.primary_output_id)
    obs = list(session.scalars(rows_q))
    prod = [o for o in obs if "PROD" in o.output_id.upper()]
    rows_obs = prod if adf and prod else obs
    rows_scope = "PROD tables" if adf and prod else ("all tables" if adf else "all outputs")
    for o in rows_obs:
        if o.observed_at >= start and (d := day_of(o.observed_at)) in daily:
            daily[d]["rows"] += o.value
    week = now - timedelta(days=7)
    rows_7d = sum(o.value for o in rows_obs if o.observed_at >= week)
    rows_prev_7d = sum(o.value for o in rows_obs if week - timedelta(days=7) <= o.observed_at < week)

    # ── why runs failed ──
    reasons: dict[str, dict[str, Any]] = {}
    import re as _re

    for r in finished(cur):
        if r.run_status == "Succeeded":
            continue
        m = _re.search(r"Operation on target (.+?) failed(?:: (.*))?$", (r.error or "").strip(), _re.S)
        step, msg = (m.group(1), m.group(2) or "") if m else (None, r.error or "")
        if not msg.strip() or "inner activity failed" in msg:
            reason = f"a step inside '{step}' failed" if step else "failed without an error message"
        else:
            reason = kind_of(msg)
        k = reasons.setdefault(reason, {"reason": reason, "count": 0, "last": None, "steps": Counter(), "pipelines": Counter()})
        k["count"] += 1
        k["last"] = max(k["last"] or r.requested_at, r.requested_at)
        if step:
            k["steps"][step] += 1
        k["pipelines"][r.site] += 1
    failure_reasons = [{"reason": v["reason"], "count": v["count"], "last": iso(v["last"]),
                        "steps": [s for s, _ in v["steps"].most_common(3)], "pipelines": [p for p, _ in v["pipelines"].most_common(3)]}
                       for v in sorted(reasons.values(), key=lambda v: -v["count"])]

    # ── pipelines ──
    pipelines = []
    for site in sorted({r.site for r in cur}):
        mine = sorted(finished([r for r in cur if r.site == site]), key=lambda r: r.requested_at, reverse=True)
        if not mine:
            continue
        durations = [r.duration_s for r in mine if r.duration_s]
        streak = 0
        for r in mine:
            if r.run_status == "Succeeded":
                break
            streak += 1
        last_ok = next((r for r in mine if r.run_status == "Succeeded"), None)
        pipelines.append({
            "name": site, "runs": len(mine), "failed": sum(r.run_status != "Succeeded" for r in mine), "success_rate": rate(mine),
            "last_run_at": iso(mine[0].requested_at), "last_status": mine[0].run_status, "failing_streak": streak,
            "last_success_at": iso(last_ok.requested_at) if last_ok else None,
            "median_duration_s": statistics.median(durations) if durations else None,
            "trigger": Counter((r.pipeline or "").split(" · ")[-1] for r in mine if r.pipeline).most_common(1)[0][0] if any(r.pipeline for r in mine) else None,
        })

    # every available pipeline / workflow, including those with no run in the period (or never)
    if adf:
        from ..engine.adf import inventory_of

        inv_all = inventory_of(session, project.key)
        inv = {i["name"]: i for i in inv_all.get("items", [])}
        if inv and not inv_all.get("error"):  # renamed or deleted in Azure: not a pipeline of this project any more
            pipelines = [p for p in pipelines if p["name"] in inv]
        have = {p["name"] for p in pipelines}
        for name in inv:
            if name in have:
                continue
            last = session.scalars(select(Run).where(Run.project_id == project.id, Run.site == name)
                                   .order_by(Run.requested_at.desc()).limit(1)).first()
            pipelines.append({"name": name, "runs": 0, "failed": 0, "success_rate": None,
                              "last_run_at": iso(last.requested_at) if last else None, "last_status": last.run_status if last else None,
                              "failing_streak": 0, "last_success_at": None, "median_duration_s": None, "trigger": None})
        for p in pipelines:
            i = inv.get(p["name"], {})
            p.update({"state": i.get("state", "Enabled"), "folder": i.get("folder"), "steps": i.get("steps"),
                      "triggers": i.get("triggers", []), "watched": i.get("watched", True), "type": i.get("type", "pipeline")})
        pipelines.sort(key=lambda p: (p["runs"] == 0, p["name"].lower()))

    # ── tables (ADF): a delivery scorecard ──
    tables = []
    if adf:
        for t in loads_view(session, project, cfg, days=max(days, 7))["tables"]:
            age_h = (now - datetime.fromisoformat(t["last"]["at"].replace("Z", "+00:00")).replace(tzinfo=None)).total_seconds() / 3600
            n = t["normal"]
            rows = t["last"]["rows_copied"]
            state = ("issue" if t["status"] != "ok" else "late" if age_h > 26 else
                     "unusual" if n and rows is not None and (rows < n["low"] or rows > n["high"]) else "ok")
            name = t["table"].split(".")[-1]
            tables.append({"name": name, "table": t["table"], "env": t["env"], "pipeline": t["pipeline"], "state": state,
                           "last_at": t["last"]["at"], "age_hours": round(age_h, 1), "rows": rows,
                           "normal": n["median"] if n else None, "incident": t["incident"]})

    # ── data quality inside the tables ──
    dq = quality_view(session, project, cfg, days) if adf else None
    if dq:
        by_table = {t["table"].upper(): t for t in dq["tables"]}
        for t in tables:
            d = by_table.get(t["table"].upper())
            t["quality"] = {"status": d["status"], "score": d["score"], "failing": [c["label"] for c in d["checks"] if c["status"] == "fail"]} if d else None

    # ── incidents ──
    inc_all = list(session.scalars(select(Incident).where(Incident.project_id == project.id,
                                                          (Incident.status.in_(ACTIVE)) | (Incident.opened_at >= prev_start))))
    active = sorted([i for i in inc_all if i.status in ACTIVE], key=lambda i: (-SEVERITY_RANK.get(i.severity, 0), i.opened_at))
    fixed = [i for i in inc_all if i.resolved_at and i.resolved_at >= start]
    mttr = statistics.mean((i.resolved_at - i.opened_at).total_seconds() / 3600 for i in fixed) if fixed else None
    opened = [i for i in inc_all if i.opened_at >= start]
    opened_prev = [i for i in inc_all if prev_start <= i.opened_at < start]
    last_full = max((r.requested_at for r in cur if r.run_status == "Succeeded"), default=None)
    if last_full is None:
        last_full = session.scalar(select(func.max(Run.requested_at)).where(Run.project_id == project.id, Run.run_status == "Succeeded"))

    # ── the one-line answer ── (the same attention model as the dock, Home and the register: an acknowledged incident,
    # or a failure a person resolved before the next run, is being handled; it's said, not flagged)
    reasons_txt: list[str] = []
    fresh = [i for i in active if i.status == "open"]
    crit = sum(i.severity == "critical" for i in fresh)
    behind = [t for t in tables if t["state"] in ("issue", "late")]
    site_open = {i.scope.removeprefix("site:"): i for i in active if i.scope.startswith("site:")}
    site_closed: dict[str, Incident] = {}
    for i in session.scalars(select(Incident).where(Incident.project_id == project.id, Incident.status == "resolved",
                                                    Incident.scope.like("site:%"), Incident.resolved_at >= start)
                             .order_by(Incident.resolved_at.desc())):
        site_closed.setdefault(i.scope.removeprefix("site:"), i)
    last_run = {r.site: r for r in sorted(cur, key=lambda r: r.requested_at)}
    unhandled_streak = False
    for p in pipelines:
        failed = p["last_status"] in ("Failed", "Cancelled")
        h = handled_by(site_open.get(p["name"]), site_closed.get(p["name"]), last_run.get(p["name"]) if failed else None)
        p["handled"] = h
        on_it = (f" {h['by'] or 'Someone'} acknowledged it." if h and h["kind"] == "acknowledged"
                 else f" {h['by']} resolved it{' (' + chr(8220) + h['note'] + chr(8221) + ')' if h['note'] else ''}; the next run will confirm." if h else "")
        if p["failing_streak"] >= 2:
            reasons_txt.append(f"{p['name']} has failed its last {p['failing_streak']} runs.{on_it}")
            unhandled_streak = unhandled_streak or (h is None and p["failing_streak"] >= 3)
        elif failed:
            reasons_txt.append(f"{p['name']}'s latest run failed.{on_it}")
    if behind:
        envs = Counter(t["env"] or "?" for t in behind)
        reasons_txt.append(f"{len(behind)} table loads are late or need a look ({', '.join(f'{n} {e}' for e, n in envs.items())}).")
    if crit:
        reasons_txt.append(f"{crit} critical incident{'s' if crit > 1 else ''} need{'' if crit > 1 else 's'} attention (nobody has acknowledged {'them' if crit > 1 else 'it'} yet).")
    if len(active) > len(fresh):
        who = sorted({i.acknowledged_by for i in active if i.status == "acknowledged" and i.acknowledged_by})
        n = len(active) - len(fresh)
        reasons_txt.append(f"{n} incident{'s are' if n > 1 else ' is'} acknowledged and being handled{' by ' + ', '.join(who) if who else ''}.")
    if dq and dq["summary"]["failing"]:
        reasons_txt.append(f"{dq['summary']['failing']} table{'s' if dq['summary']['failing'] > 1 else ''} failed data-quality checks.")
    level = "critical" if crit or unhandled_streak else "warning" if active or behind or reasons_txt else "ok"
    success = rate(cur)
    ok_tables = sum(t["state"] in ("ok", "unusual") for t in tables)
    rel = f"Pipelines succeeded {success:.0f}% of the time in the last {days} days" if success is not None else ""
    deliv = f"{ok_tables} of {len(tables)} tables received data in the last day" if tables else ""
    if level == "ok":  # good news first
        headline = " · ".join(x for x in (deliv or "All pipelines are healthy", rel) if x)
    else:  # the problem first, then what still worked
        headline = " · ".join(x for x in (rel or "Pipelines need attention", deliv.replace("tables received", "tables still received") if deliv else "") if x)
        if tables and ok_tables == len(tables) and any(p["failed"] for p in pipelines):
            reasons_txt.append("Tables still got data because a failed run only fails some of its steps; the others load as normal.")

    # ── one data health score (0-100): pipelines that succeed, tables that arrive, data that passes its checks ──
    parts = {"pipelines": success, "delivery": round(100 * ok_tables / len(tables), 1) if tables else None,
             "quality": dq["summary"]["score"] if dq else None}
    weights = {"pipelines": 0.4, "delivery": 0.3, "quality": 0.3}
    have = {k: v for k, v in parts.items() if v is not None}
    health = round(sum(v * weights[k] for k, v in have.items()) / sum(weights[k] for k in have)) if have else None

    return {
        "project": {"key": project.key, "name": project.display_name, "kind": cfg.kind, "description": project.description},
        "period_days": days, "generated_at": iso(now),
        "health": {"score": health, "parts": parts, "weights": weights},
        "quality": {"enabled": dq["enabled"], "status": dq["status"], "summary": dq["summary"]} if dq else None,
        "status": {"level": level, "headline": headline, "reasons": reasons_txt or ["Every pipeline run and table load is as expected."]},
        "kpis": {
            "success_rate": success, "success_rate_prev": rate(prev),
            "runs": len(finished(cur)), "failed": sum(r.run_status != "Succeeded" for r in finished(cur)),
            "runs_prev": len(finished(prev)), "failed_prev": sum(r.run_status != "Succeeded" for r in finished(prev)),
            "tables": len(tables), "tables_on_time": sum(t["state"] in ("ok", "unusual") for t in tables),
            "tables_behind": len(behind), "rows_7d": rows_7d, "rows_prev_7d": rows_prev_7d, "rows_scope": rows_scope,
            "open_critical": crit, "open_warning": len(fresh) - crit, "being_handled": len(active) - len(fresh),
            "incidents_opened": len(opened), "incidents_opened_prev": len(opened_prev),
            "mttr_hours": round(mttr, 1) if mttr is not None else None, "fixed": len(fixed),
            "last_full_success_at": iso(last_full),
        },
        "daily": list(daily.values()),
        "failure_reasons": failure_reasons,
        "pipelines": pipelines,
        "tables": tables,
        "incidents": [{**incident_brief(i), "age_hours": round((now - i.opened_at).total_seconds() / 3600, 1)} for i in active[:10]],
    }


DQ_CHECKS = ("row_count", "null_spike", "duplicates", "freshness", "schema_drift", "value_range", "out_of_range", "invalid_type",
             "distribution_shift", "business_rule", "referential_integrity")
DQ_LABELS = {"row_count": "Row count", "null_spike": "Empty values", "duplicates": "Duplicates", "freshness": "Freshness",
             "schema_drift": "Schema", "value_range": "Value ranges", "business_rule": "Business rules",
             "referential_integrity": "References", "distribution_shift": "Distribution", "out_of_range": "Allowed ranges",
             "invalid_type": "Data types"}


def quality_view(session: Session, project: Project, cfg: ProjectConfig, days: int = 30) -> dict[str, Any]:
    """ADF projects: data quality inside every table the pipelines load. Per table: the latest result of each check,
    the measured values (rows today, empty % per column, duplicates, age of the newest row), its columns and history."""
    from ..dq.service import mapping_view, mappings_of, shown_mappings
    from ..engine.quality import DQ_PREFIX, get_status
    from .dq_views import project_quality

    q = cfg.quality
    maps = shown_mappings(mappings_of(session, project))
    status = get_status(session, project.key)
    since = utcnow() - timedelta(days=days)
    results = session.scalars(select(CheckResult).where(
        CheckResult.project_id == project.id, CheckResult.scope.like("data:%"), CheckResult.created_at >= since,
        CheckResult.status.in_(("pass", "fail"))).order_by(CheckResult.created_at.desc(), CheckResult.id.desc()))
    latest: dict[str, dict[str, Any]] = defaultdict(dict)
    for r in results:
        key = r.check + ((":" + str(r.details.get("rule") or r.details.get("reference") or "")) if r.check in ("business_rule", "referential_integrity") else "")
        latest[r.scope.removeprefix("data:")].setdefault(key, r)
    obs = session.scalars(select(MetricObservation).where(
        MetricObservation.project_id == project.id, MetricObservation.output_id.like(f"{DQ_PREFIX}%"),
        MetricObservation.observed_at >= since).order_by(MetricObservation.observed_at))
    runs: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for o in obs:
        per = runs[o.output_id.removeprefix(DQ_PREFIX)].setdefault(o.run_ref, {"at": o.observed_at, "pipeline": o.site, "values": {}})
        per["values"][(o.metric, o.field)] = o.value
    active = {i.scope: i for i in session.scalars(select(Incident).where(Incident.project_id == project.id, Incident.status.in_(ACTIVE)))}
    tables = []
    for table in sorted(set(runs) | set(latest)):
        hist = sorted(runs.get(table, {}).values(), key=lambda h: h["at"])
        last = hist[-1] if hist else None
        v = last["values"] if last else {}
        checks = []
        for key, r in sorted(latest.get(table, {}).items(), key=lambda kv: DQ_CHECKS.index(kv[1].check) if kv[1].check in DQ_CHECKS else 99):
            label = DQ_LABELS.get(r.check, r.check)
            if ":" in key and key.split(":", 1)[1]:
                label = f"{label}: {key.split(':', 1)[1]}"
            checks.append({"check": r.check, "label": label, "status": r.status, "severity": r.severity, "message": r.message,
                           "at": iso(r.created_at)})
        nulls = sorted(({"column": f, "rate": val} for (m, f), val in v.items() if m == "null_rate"), key=lambda x: -x["rate"])
        snap = session.scalars(select(SchemaSnapshot).where(SchemaSnapshot.project_id == project.id, SchemaSnapshot.output_id == f"{DQ_PREFIX}{table}")
                               .order_by(SchemaSnapshot.captured_at.desc()).limit(1)).first()
        failing = [c for c in checks if c["status"] == "fail"]
        inc = active.get(f"data:{table}")
        score = 100 - sum(30 if c["severity"] == "critical" else 15 for c in failing)
        tables.append({
            "table": table, "name": table.split(".")[-1], "pipeline": last["pipeline"] if last else None,
            "checked_at": iso(last["at"]) if last else None,
            "status": "fail" if failing else ("ok" if checks else "learning"),
            "score": max(0, score) if checks else None,
            "rows_today": v.get(("row_count", "")), "rows_total": v.get(("rows_total", "")),
            "mapping": next((mapping_view(m) for m in maps if f"{m.connection.database}.{m.schema_name}.{m.table_name}".upper() == table), None),
            "freshness_hours": v.get(("freshness_hours", "")), "duplicate_rate": v.get(("duplicate_rate", "")),
            "nulls": nulls[:12], "columns": len(snap.columns) if snap else None,
            "schema": [{"name": c["name"], "type": c["type"]} for c in (snap.columns if snap else [])],
            "checks": checks, "history": [{"at": iso(h["at"]), "rows": h["values"].get(("row_count", ""))} for h in hist[-30:]],
            "incident": {"id": inc.id, "title": inc.title, "severity": inc.severity} if inc else None,
        })
    scored = [t["score"] for t in tables if t["score"] is not None]
    legacy = q is not None and q.enabled
    report = session.get(BotState, f"dq-report:{project.key}")
    return {
        "enabled": legacy or bool(maps),
        "source": {"type": q.source.type, "account": q.source.account, "warehouse": q.source.warehouse,
                   "authenticator": q.source.authenticator} if legacy and q else None,
        "mappings": [mapping_view(m) for m in maps],
        "report": report.value if report else None,
        "overview": project_quality(session, project, sorted(set(runs) | set(latest)), maps),
        "status": status, "tables": tables,
        "summary": {"tables": len(tables), "failing": sum(t["status"] == "fail" for t in tables),
                    "checks": sum(len(t["checks"]) for t in tables),
                    "checks_failing": sum(c["status"] == "fail" for t in tables for c in t["checks"]),
                    "score": round(sum(scored) / len(scored)) if scored else None},
    }


SERVICE = {"adf": "Data Factory", "synapse": "Synapse", "logic_apps": "Logic Apps"}


def pipelines_view(session: Session, projects: list[tuple[Project, str]], days: int = 7) -> dict[str, Any]:
    """Every pipeline and Logic App across the person's projects, run or not: where it lives, how it's triggered, its
    latest run, the last `days` days, and whether something is open on it."""
    from ..engine.adf import inventory_of

    cfgs = load_projects()
    since = utcnow() - timedelta(days=days)
    items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    # every Logic App the person can see, by workflow id (to name what pipelines call)
    workflows: dict[str, dict[str, str]] = {}
    for project, _ in projects:
        c = cfgs.get(project.key)
        if c is not None and c.pipeline is not None and c.pipeline.type == "logic_apps":
            for w in inventory_of(session, project.key).get("items", []):
                if w.get("workflow_id"):
                    workflows[w["workflow_id"]] = {"name": w["name"], "project": project.key, "project_name": project.display_name}
    for project, role in projects:
        cfg = cfgs.get(project.key)
        if cfg is None or cfg.kind != "adf" or cfg.pipeline is None:
            continue
        inv = inventory_of(session, project.key)
        runs = list(session.scalars(select(Run).where(Run.project_id == project.id, Run.requested_at >= since)
                                    .order_by(Run.requested_at.desc())))
        latest: dict[str, Run] = {}
        for r in session.scalars(select(Run).where(Run.project_id == project.id).order_by(Run.requested_at.desc()).limit(2000)):
            latest.setdefault(r.site, r)
        open_inc = {i.scope.removeprefix("site:"): i for i in session.scalars(select(Incident).where(
            Incident.project_id == project.id, Incident.status.in_(ACTIVE), Incident.scope.like("site:%")))}
        # the newest resolved incident per pipeline: a person may have resolved a failure before the next run
        closed: dict[str, Incident] = {}
        for i in session.scalars(select(Incident).where(Incident.project_id == project.id, Incident.status == "resolved",
                                                        Incident.scope.like("site:%"), Incident.resolved_at >= since)
                                 .order_by(Incident.resolved_at.desc())):
            closed.setdefault(i.scope.removeprefix("site:"), i)
        service = SERVICE.get(cfg.pipeline.type, cfg.pipeline.type)
        norms = _norms(session, project, cfg)
        known = {i["name"]: i for i in inv.get("items", [])}
        current = bool(known) and not inv.get("error")  # Azure's own list is fresh: show exactly what exists there now
        ran = {r.site for r in runs}
        names = sorted(set(known) if current else set(known) | ran)
        gone = sorted(ran - set(known)) if current else []  # renamed or deleted in Azure; their history stays in Runs
        sources.append({"project": project.key, "project_name": project.display_name, "service": service, "role": role,
                        "count": len(names), "checked_at": inv.get("at"), "error": inv.get("error"), "gone": gone})
        for n in names:
            i = known.get(n, {})
            mine = [r for r in runs if r.site == n]
            last = latest.get(n)
            done = [r for r in mine if r.run_status in ("Succeeded", "Failed", "Cancelled")]
            inc = open_inc.get(n)
            failed_last = last is not None and last.run_status in ("Failed", "Cancelled")
            handled = handled_by(inc, closed.get(n), last if failed_last else None)
            state = ("disabled" if i.get("state") == "Disabled" else "running" if last is not None and last.run_status in ("InProgress", "Queued")
                     else "handled" if handled is not None
                     else "failing" if inc is not None or failed_last
                     else "never_run" if last is None else "idle" if not mine
                     else "unstable" if any(r.run_status in ("Failed", "Cancelled") for r in done) else "healthy")
            conns = []
            for c in i.get("connections", []):
                c = dict(c)
                if c["kind"] == "logic_app" and (w := workflows.get(c.get("workflow_id") or "")):
                    c.update(name=w["name"], project=w["project"], project_name=w["project_name"])
                elif c["kind"] == "logic_app":
                    c["name"] = f"Logic App {c['name'][:8]}…"  # a workflow this person can't see in Azure
                conns.append(c)
            items.append({
                "connections": conns,
                "name": n, "project": project.key, "project_name": project.display_name, "service": service,
                "type": i.get("type", "logic_app" if cfg.pipeline.type == "logic_apps" else "pipeline"),
                "folder": i.get("folder"), "description": i.get("description") or "", "steps": i.get("steps"),
                "triggers": i.get("triggers", []), "enabled": i.get("state", "Enabled") != "Disabled", "watched": i.get("watched", True),
                "state": state,
                "runtime": (norms.compare(n, last.duration_s, status=last.run_status, started_at=last.started_at) if last else norms.of(n))
                if norms else None,
                "last_run": {"at": iso(last.requested_at), "status": last.run_status, "duration_s": last.duration_s,
                             "run_id": last.run_id} if last else None,
                "runs": len(mine), "failed": sum(r.run_status in ("Failed", "Cancelled") for r in done),
                "succeeded": sum(r.run_status == "Succeeded" for r in done),
                "incident": {"id": inc.id, "title": inc.title, "severity": inc.severity, "status": inc.status} if inc else None,
                "handled": handled,
            })
    # the reverse: which pipelines call each Logic App
    for it in items:
        for c in it["connections"]:
            if c["kind"] == "logic_app" and c.get("project"):
                target = next((x for x in items if x["project"] == c["project"] and x["name"] == c["name"]), None)
                if target is not None:
                    target.setdefault("called_by", []).append({"name": it["name"], "project": it["project"], "project_name": it["project_name"]})
    order = {"failing": 0, "handled": 1, "unstable": 2, "running": 3, "healthy": 4, "idle": 5, "never_run": 6, "disabled": 7}
    items.sort(key=lambda x: (order[x["state"]], x["project_name"].lower(), x["name"].lower()))
    counts = {k: sum(x["state"] == k for x in items) for k in order}
    return {"days": days, "items": items, "sources": sources, "counts": {"total": len(items), **counts},
            "services": {s: sum(x["service"] == s for x in items) for s in SERVICE.values()}}


def handled_by(active: Incident | None, resolved: Incident | None, failed_run: Run | None) -> dict[str, Any] | None:
    """Someone is dealing with this pipeline's problem: its open incident is acknowledged, or its last run failed and a
    person resolved the incident after that ("re-ran it", "rescheduled"), so the next run will tell. None otherwise."""
    if active is not None:
        if active.status != "acknowledged":
            return None
        return {"kind": "acknowledged", "by": active.acknowledged_by, "at": iso(active.acknowledged_at), "incident_id": active.id,
                "title": active.title, "note": None}
    if failed_run is not None and resolved is not None and resolved.resolved_by and resolved.resolved_by != "Alert Bot" \
            and resolved.resolved_at is not None and resolved.resolved_at >= failed_run.requested_at:
        return {"kind": "resolved", "by": resolved.resolved_by, "at": iso(resolved.resolved_at), "incident_id": resolved.id,
                "title": resolved.title, "note": resolved.resolution}
    return None


def lineage_view(session: Session, projects: list[tuple[Project, str]]) -> dict[str, Any]:
    """Data → pipelines → data, across platforms, factories and projects: what each pipeline reads and writes (and runs or
    calls), merged so a table one pipeline writes and another reads is one node. With each pipeline's state and each
    table's open incident, so the portal can show what a failure affects downstream."""
    from ..engine.adf import inventory_of

    pv = pipelines_view(session, projects, days=7)
    state = {(i["project"], i["name"]): i for i in pv["items"]}
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def data_node(t: dict[str, Any], project: Project) -> str:
        host = (t.get("host") or "").lower()
        base = f"data:{(t.get('platform') or 'data').lower()}:{(t.get('table') or '').lower()}"
        nid = f"{base}@{host}" if host else base
        n = nodes.setdefault(nid, {"id": nid, "kind": "data", "data_kind": t.get("kind", "table"), "platform": t.get("platform") or "Data",
                                    "host": t.get("host"), "database": t.get("database"), "object": t.get("table"), "env": t.get("env"),
                                    "dynamic": bool(t.get("dynamic")), "dataset": t.get("dataset"), "procedure": t.get("procedure"),
                                    "projects": []})
        if project.display_name not in n["projects"]:
            n["projects"].append(project.display_name)
        return nid

    def edge(a: str, b: str, kind: str, step: str, **extra: Any) -> None:
        e = edges.setdefault((a, b, kind), {"from": a, "to": b, "kind": kind, "steps": [], **extra})
        if step and step not in e["steps"]:
            e["steps"].append(step)

    def note(nid: str, key: str, value: str) -> None:
        if value not in nodes[nid].setdefault(key, []):
            nodes[nid][key].append(value)

    cfgs = load_projects()
    for project, _ in projects:
        cfg = cfgs.get(project.key)
        if cfg is None or cfg.kind != "adf":
            continue
        for item in inventory_of(session, project.key).get("items", []):
            pid = f"pipeline:{project.key}:{item['name']}"
            st = state.get((project.key, item["name"]), {})
            nodes[pid] = {"id": pid, "kind": "pipeline", "label": item["name"], "project": project.key, "project_name": project.display_name,
                          "service": st.get("service"), "type": item.get("type", "pipeline"), "state": st.get("state", "never_run"),
                          "last_run": st.get("last_run"), "incident": st.get("incident")}
            steps = item.get("lineage") or []
            # A pipeline that writes something and reads it too is staging there (lands files, then loads them), or
            # updating it in place: that's one hop out of the pipeline, not a loop back into it. A Delete step tidies
            # up what was read: noted on the data, not drawn as data flowing anywhere.
            written = {data_node(t, project) for s in steps if s.get("type") != "Delete" for t in s.get("writes") or []}
            for step in steps:
                for t in step.get("reads") or []:
                    nid = data_node(t, project)
                    if nid in written:
                        note(nid, "staged_by", item["name"])
                    else:
                        edge(nid, pid, "reads", step["step"])
                for t in step.get("writes") or []:
                    nid = data_node(t, project)
                    if step.get("type") == "Delete":
                        note(nid, "cleaned_by", item["name"])
                    else:
                        edge(pid, nid, "writes", step["step"])
                if step.get("runs"):
                    edge(pid, f"pipeline:{project.key}:{step['runs']}", "runs", step["step"])
            for nid in written:
                if item["name"] in nodes[nid].get("staged_by", []):
                    edges[(pid, nid, "writes")]["staging"] = True
            for c in st.get("connections", []):
                if c["kind"] not in ("logic_app", "function_app", "databricks", "notebook"):
                    continue
                if c.get("project"):  # a Logic App this person can see: link to it
                    target = f"pipeline:{c['project']}:{c['name']}"
                else:
                    target = f"service:{c['kind']}:{c['name'].lower()}"
                    nodes.setdefault(target, {"id": target, "kind": "service", "service_kind": c["kind"], "label": c["name"],
                                              "function": c.get("function")})
                edge(pid, target, "calls", ", ".join(c.get("steps", [])))

    # a table seen without its server (connection didn't say) is the same table as the one seen with it
    by_base: dict[str, list[str]] = defaultdict(list)
    for nid in nodes:
        if nid.startswith("data:"):
            by_base[nid.split("@", 1)[0]].append(nid)
    alias = {}
    for base, ids in by_base.items():
        hosted = [i for i in ids if "@" in i]
        if base in ids and len(hosted) == 1:
            alias[base] = hosted[0]
            for k in ("projects", "staged_by", "cleaned_by"):
                merged_list = sorted(set(nodes[hosted[0]].get(k, [])) | set(nodes[base].get(k, [])))
                if merged_list:
                    nodes[hosted[0]][k] = merged_list
            del nodes[base]
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for (a, b, k), e in edges.items():
        a, b = alias.get(a, a), alias.get(b, b)
        if a not in nodes or b not in nodes:
            continue
        m = merged.setdefault((a, b, k), {**e, "from": a, "to": b, "steps": []})
        m["steps"] = list(dict.fromkeys(m["steps"] + e["steps"]))

    # tables with an open incident
    issues = {}
    for i in session.scalars(select(Incident).where(Incident.project_id.in_([p.id for p, _ in projects] or [-1]), Incident.status.in_(ACTIVE))):
        kind, _, name = i.scope.partition(":")
        if kind in ("table", "data"):
            issues[name.upper()] = {"id": i.id, "title": i.title, "severity": i.severity, "project": next(p.key for p, _ in projects if p.id == i.project_id)}
    # tables the bot checks the data of (a live mapping on someone's connected database)
    from ..dq.service import canonical, checked

    dq = {canonical(m.connection, m.schema_name, m.table_name) for m in session.scalars(
        select(DqMapping).where(DqMapping.project_id.in_([p.id for p, _ in projects] or [-1]))) if checked(m)}
    for n in nodes.values():
        if n["kind"] == "data":
            n["quality_checked"] = (n.get("object") or "").upper() in dq
            n["incident"] = issues.get((n.get("object") or "").upper())
            obj = n.get("object") or ""
            # a table by its own name; files by container/folder, since "inbound" alone says nothing about whose
            n["label"] = ("/".join(obj.split("/")[-2:]) if n.get("data_kind") == "files" else obj.split(".")[-1]) or n.get("dataset") or n["platform"]

    platforms = sorted({n["platform"] for n in nodes.values() if n["kind"] == "data"})
    return {"nodes": list(nodes.values()), "edges": list(merged.values()), "platforms": platforms,
            "projects": [{"key": p.key, "name": p.display_name} for p, _ in projects],
            "summary": {"pipelines": sum(n["kind"] == "pipeline" for n in nodes.values()), "data": sum(n["kind"] == "data" for n in nodes.values()),
                        "platforms": len(platforms), "failing": [n["id"] for n in nodes.values() if n["kind"] == "pipeline" and n["state"] in ("failing", "handled")]}}


def mutes_list(session: Session, project: Project) -> list[dict[str, Any]]:
    return [
        {"id": m.id, "scope": m.scope, "reason": m.reason, "until": iso(m.until), "created_by": m.created_by,
         "created_at": iso(m.created_at), "active": m.until > utcnow()}
        for m in session.scalars(select(Mute).where(Mute.project_id == project.id).order_by(Mute.created_at.desc()).limit(50))
    ]


def config_view(cfg: ProjectConfig) -> dict[str, Any]:
    """The project's config as the portal may show it: references only, never secret values."""
    return {
        "project": cfg.project, "display_name": cfg.display_name, "schedule": cfg.schedule,
        "kind": cfg.kind, "timezone": cfg.timezone,
        "pipeline": {"type": cfg.pipeline.type, "factory": cfg.pipeline.factory, "pipelines": cfg.pipeline.pipelines,
                     "resource_group": cfg.pipeline.resource_group} if cfg.pipeline else None,
        "trigger_table": {"table": cfg.trigger_table.table, "statuses": cfg.trigger_table.statuses.model_dump()}
        if cfg.trigger_table else None,
        "outputs": [o.model_dump() for o in cfg.outputs],
        "feeds": [f.model_dump() for f in cfg.feeds],
        "checks": {name: {"enabled": s.enabled, "severity": s.severity, **(s.model_extra or {})} for name, s in cfg.checks.items()},
        "alerts": notifier_info(cfg),
    }
