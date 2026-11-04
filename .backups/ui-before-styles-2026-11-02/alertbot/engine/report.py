"""The Daily Morning Report: one message per project each morning, so nobody has to open the portal to know whether
the data is ready.

    Data Health Score: 92/100
    Pipelines (last 24 h): 45 completed · 2 delayed · 1 failed
    Tables checked: 120 · anomalies found: 4
    Critical issues: 1 · warnings: 3
    Trends vs the week before, then a short AI summary (checked: every number must come from the report).

Sent through the same outbox as alerts (event `report.morning`, once per project per day), shown in the portal and
printed by `alertbot morning-report`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import ProjectConfig, load_projects
from ..db import Incident, Notification, Project, Run, session_scope
from ..timeutil import iso, utcnow

log = logging.getLogger(__name__)

EVENT = "report.morning"
DELAY_CHECKS = {"missed_schedule", "run_too_slow", "freshness", "late"}


def build_report(session: Session, project: Project, cfg: ProjectConfig, now: datetime | None = None) -> dict[str, Any]:
    from ..api.views import ACTIVE, incident_brief, management_view
    from ..settings import get_settings

    now = now or utcnow()
    m = management_view(session, project, cfg, days=7)
    since = now - timedelta(hours=24)
    runs = list(session.scalars(select(Run).where(Run.project_id == project.id, Run.requested_at >= since)))
    active = list(session.scalars(select(Incident).where(Incident.project_id == project.id, Incident.status.in_(ACTIVE))))
    delayed = sorted({i.scope.split(":", 1)[-1] for i in active if DELAY_CHECKS & set(i.checks)})
    tables = m["tables"]
    q = (m.get("quality") or {}).get("summary") or {}
    anomalies = q.get("checks_failing", 0) + sum(t["state"] in ("issue", "unusual") for t in tables)
    k = m["kpis"]
    tz = get_settings().tz
    local = now.replace(tzinfo=UTC).astimezone(tz)

    def trend(label: str, cur: float | None, prev: float | None, unit: str = "", better: str = "up") -> dict[str, Any] | None:
        if cur is None or prev is None:
            return None
        diff = cur - prev
        good = diff == 0 or (diff > 0) == (better == "up")
        return {"label": label, "now": cur, "before": prev, "unit": unit, "direction": "same" if diff == 0 else ("up" if diff > 0 else "down"),
                "good": good}

    trends = [t for t in (
        trend("Pipeline success rate", k["success_rate"], k["success_rate_prev"], "%"),
        trend("Failed runs", k["failed"], k["failed_prev"], better="down"),
        trend("Rows delivered", k["rows_7d"], k["rows_prev_7d"]),
        trend("Incidents opened", k["incidents_opened"], k["incidents_opened_prev"], better="down"),
    ) if t]
    crit = sorted((i for i in active if i.severity == "critical"), key=lambda i: i.opened_at)
    warn = sorted((i for i in active if i.severity != "critical"), key=lambda i: i.opened_at)
    report = {
        "project": {"key": project.key, "name": project.display_name, "kind": cfg.kind},
        "date": local.date().isoformat(), "generated_at": iso(now),
        "health": m["health"], "status": m["status"],
        "pipelines": {"completed": sum(r.run_status == "Succeeded" for r in runs),
                      "failed": sum(r.run_status in ("Failed", "Cancelled") for r in runs),
                      "running": sum(r.run_status in ("InProgress", "Queued") for r in runs),
                      "delayed": len(delayed), "delayed_names": delayed[:8]},
        "tables": {"loaded": len(tables), "on_time": k.get("tables_on_time", 0), "checked": q.get("tables", 0),
                   "anomalies": anomalies, "failing_quality": q.get("failing", 0)},
        "issues": {"critical": len(crit), "warning": len(warn),
                   "list": [incident_brief(i) for i in (crit + warn)[:8]]},
        "trends": trends,
        "ai_summary": None,
        "url": f"{get_settings().portal_url.rstrip('/')}/p/{project.key}",
    }
    try:  # Snowflake compute, when a member of this project connected an account
        from ..dq.compute import morning_line

        report["snowflake"] = morning_line(session, project, now)
    except Exception:  # noqa: BLE001 - a report without it is still a report
        report["snowflake"] = None
    report["text"] = report_text(report)
    return report


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0f}%"


def _trend_value(tr: dict[str, Any]) -> str:
    fmt = (lambda v: f"{v:.0f}%") if tr["unit"] == "%" else (lambda v: f"{v:,.0f}")  # noqa: E731
    arrow = {"up": "↑", "down": "↓", "same": "→"}[tr["direction"]]
    return f"{fmt(tr['now'])} {arrow} (was {fmt(tr['before'])})"


def report_text(r: dict[str, Any]) -> str:
    p, t, i = r["pipelines"], r["tables"], r["issues"]
    score = r["health"]["score"]
    lines = [f"Daily data report · {r['project']['name']} · {r['date']}", "",
             f"Data Health Score: {score}/100" if score is not None else "Data Health Score: not enough data yet",
             f"Pipelines (last 24 h): {p['completed']} completed · {p['delayed']} delayed · {p['failed']} failed"
             + (f" · {p['running']} running" if p["running"] else ""),
             f"Tables: {t['loaded']} loaded, {t['on_time']} on time" + (f" · {t['checked']} quality-checked" if t["checked"] else "")
             + f" · anomalies found: {t['anomalies']}",
             f"Critical issues: {i['critical']} · warnings: {i['warning']}"]
    if i["list"]:
        lines += ["", "Needs attention:"] + [f"• [{x['severity']}] {x['title']}" for x in i["list"]]
    if p["delayed_names"]:
        lines += ["", "Delayed: " + ", ".join(p["delayed_names"])]
    if r.get("snowflake"):
        lines += ["", r["snowflake"]]
    if r["trends"]:
        lines += ["", "Trends vs the previous week:"]
        lines += [f"• {tr['label']}: {_trend_value(tr)}" for tr in r["trends"]]
    if r.get("ai_summary"):
        lines += ["", r["ai_summary"]]
    return "\n".join(lines)


AI_PROMPT = """You write the summary paragraph of a daily data-reliability report for managers.
Use ONLY the report given. 2-3 plain sentences: is today's data usable, what is the main problem (if any) and its likely
impact, and the trend. Copy numbers and names exactly; never add numbers that aren't in the report. No markdown, no links.
Return JSON: {"summary": "..."}"""


def ai_summary(report: dict[str, Any]) -> tuple[str, str] | None:
    """(paragraph, model), or None when AI is off or its text didn't pass the check. Call with no DB session open."""
    from ..ai.llm import client, invented_numbers
    from ..config import load_ai_config

    if not load_ai_config().ready:
        return None
    facts = {k: report[k] for k in ("project", "date", "health", "status", "pipelines", "tables", "issues", "trends")}
    evidence = json.dumps(facts, default=str, ensure_ascii=False) + "\n" + report["text"]
    try:
        from ..ai.usage import tagged

        with tagged(project=(report.get("project") or {}).get("key"), feature="report"):
            out, res = client().chat_json(AI_PROMPT, f"Report:\n{evidence}", max_tokens=900)
        text = str(out.get("summary") or "").strip()
        if len(text) < 20 or "http" in text:
            raise ValueError("empty or linked summary")
        if made_up := invented_numbers(text, evidence):
            raise ValueError(f"numbers not in the report: {sorted(made_up)[:5]}")
        return text, res.model
    except Exception as exc:  # noqa: BLE001 - the report stands without it
        log.info("morning report AI summary skipped: %s", exc)
        return None


def report_card(r: dict[str, Any]) -> dict[str, Any]:
    """Teams Adaptive Card for the report."""
    score = r["health"]["score"]
    level = r["status"]["level"]
    style = {"ok": "good", "warning": "warning", "critical": "attention"}.get(level, "default")
    p, t, i = r["pipelines"], r["tables"], r["issues"]
    body: list[dict[str, Any]] = [
        {"type": "Container", "style": style, "bleed": True, "items": [
            {"type": "TextBlock", "text": f"☀️ Daily data report · {r['project']['name']} · {r['date']}", "size": "Small",
             "weight": "Bolder", "isSubtle": True, "wrap": True},
            {"type": "TextBlock", "text": f"Data Health Score: {score}/100" if score is not None else "Data Health Score: learning",
             "size": "Large", "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": r["status"]["headline"], "wrap": True, "spacing": "None"},
        ]},
        {"type": "FactSet", "facts": [
            {"title": "Pipelines (24 h)", "value": f"{p['completed']} completed · {p['delayed']} delayed · {p['failed']} failed"},
            {"title": "Tables", "value": f"{t['loaded']} loaded · {t['on_time']} on time" + (f" · {t['checked']} quality-checked" if t["checked"] else "")},
            {"title": "Anomalies", "value": str(t["anomalies"])},
            {"title": "Issues", "value": f"{i['critical']} critical · {i['warning']} warnings"},
        ]},
    ]
    if r.get("ai_summary"):
        body.append({"type": "TextBlock", "text": r["ai_summary"], "wrap": True, "spacing": "Medium"})
    if i["list"]:
        body.append({"type": "TextBlock", "text": "Needs attention", "weight": "Bolder", "spacing": "Medium"})
        body += [{"type": "TextBlock", "text": f"{'🔴' if x['severity'] == 'critical' else '🟠'} {x['title']}", "wrap": True, "spacing": "Small"}
                 for x in i["list"][:6]]
    if r.get("snowflake"):
        body.append({"type": "TextBlock", "text": f"❄️ {r['snowflake']}", "wrap": True, "spacing": "Medium"})
    if r["trends"]:
        body.append({"type": "TextBlock", "text": "Trends vs the previous week", "weight": "Bolder", "spacing": "Medium"})
        body.append({"type": "FactSet", "facts": [{"title": tr["label"], "value": _trend_value(tr)} for tr in r["trends"]]})
    return {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.5",
            "msteams": {"width": "Full"}, "body": body,
            "actions": [{"type": "Action.OpenUrl", "title": "Open the overview", "url": r["url"]}]}


def _key(project_key: str, date: str) -> str:
    return f"{project_key}:{EVENT}:{date}"


def send_report(cfg: ProjectConfig, now: datetime | None = None, *, force: bool = False, use_ai: bool = True) -> dict[str, Any] | None:
    """Build, summarise and queue today's report, then dispatch it. Once per project per day unless forced.
    Returns the report, or None when today's was already sent."""
    from .cycle import make_context
    from .notify import dispatch

    now = now or utcnow()
    with session_scope() as s:  # read
        ctx = make_context(s, cfg, now)
        report = build_report(s, ctx.project, cfg, now)
        key = _key(cfg.project, report["date"]) + (f":{now:%H%M%S}" if force else "")
        if s.scalars(select(Notification.id).where(Notification.idempotency_key == key)).first() is not None:
            return None
    if use_ai and (summary := ai_summary(report)):  # AI with no transaction open
        report["ai_summary"], report["ai_model"] = summary
        report["text"] = report_text(report)
    with session_scope() as s:  # write + send
        ctx = make_context(s, cfg, now)
        payload = {"schema_version": "1.0", "event": EVENT, "occurred_at": iso(now), "project": cfg.project,
                   "project_name": cfg.display_name, "incident_id": 0, "status": "open", "severity": {"ok": "info"}.get(report["status"]["level"], report["status"]["level"]),
                   "title": f"Daily data report · {report['date']}", "summary": report["text"], "portal_url": report["url"],
                   "teams_channel": cfg.alerts.teams_channel, "email_owners": False, "owner_emails": [], "report": report,
                   "card": report_card(report)}
        s.add(Notification(project_id=ctx.project.id, incident_id=None, event=EVENT, idempotency_key=key, payload=payload,
                           channel=cfg.alerts.notifier, created_at=now))
        s.flush()
        report["delivery"] = dispatch(ctx)  # what actually happened: sent, dry_run (no flow), retrying, failed
    return report


def due_reports(now: datetime | None = None) -> list[str]:
    """Send each active project's morning report once its local report time has passed today. For the worker."""
    from ..settings import get_settings

    now = now or utcnow()
    tz = get_settings().tz
    local = now.replace(tzinfo=UTC).astimezone(tz)
    sent = []
    for cfg in load_projects().values():
        at = cfg.alerts.morning_report
        if not cfg.active or not at:
            continue
        hh, mm = (int(x) for x in at.split(":"))
        if (local.hour, local.minute) < (hh, mm):
            continue
        with session_scope() as s:
            done = s.scalars(select(Notification.id).where(Notification.idempotency_key == _key(cfg.project, local.date().isoformat()))).first()
        if done is not None:
            continue
        try:
            if send_report(cfg, now) is not None:
                sent.append(cfg.project)
        except Exception:  # noqa: BLE001 - one project's report must not stop the others
            log.exception("morning report for %s failed", cfg.project)
    return sent
