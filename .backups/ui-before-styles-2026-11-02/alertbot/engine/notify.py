"""Alert payloads and the outbox dispatcher.

The bot only notifies when an incident opens, gets worse, or resolves, so a
bad morning is one message, not fifty. Each notification is written to the
outbox in the same transaction as the incident change, then dispatched:

    flow URL set         → POST → sent (or retried next cycle on failure)
    flow URL empty       → dry_run (stored, visible in the portal, nothing sent)
    scope muted by owner → muted
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import or_, select

from ..db import Incident, IncidentEvent, Mute, Notification, User, UserProject
from ..timeutil import iso
from .context import ProjectContext

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
MAX_ATTEMPTS = 6

_STYLE = {"critical": "attention", "warning": "warning", "info": "accent"}
_EMOJI = {"critical": "🔴", "warning": "🟠", "info": "🔵"}


def portal_link(ctx: ProjectContext, incident_id: int) -> str:
    return f"{ctx.settings.portal_url.rstrip('/')}/p/{ctx.key}/i/{incident_id}"


def owner_emails(ctx: ProjectContext) -> list[str]:
    return list(
        ctx.session.scalars(
            select(User.email)
            .join(UserProject, UserProject.user_id == User.id)
            .where(UserProject.project_id == ctx.project.id, UserProject.role == "owner")
            .order_by(User.email)
        )
    )


def build_payload(ctx: ProjectContext, incident: Incident, event: str) -> dict[str, Any]:
    severity = incident.severity
    resolved = event == "incident.resolved"
    email = (not resolved) and severity in ctx.cfg.alerts.email_owners_on
    url = portal_link(ctx, incident.id)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": event,
        "occurred_at": iso(ctx.now),
        "project": ctx.key,
        "project_name": ctx.cfg.display_name,
        "incident_id": incident.id,
        "severity": severity,
        "status": incident.status,
        "title": incident.title,
        "summary": incident.summary,
        "impact": incident.impact,
        "likely_cause": incident.likely_cause,
        "action": incident.suggested_action,
        "confidence": incident.confidence,
        "analysis_source": incident.analysis_source,
        "checks": incident.checks,
        "scope": incident.scope,
        "opened_at": iso(incident.opened_at),
        "resolved_at": iso(incident.resolved_at),
        "portal_url": url,
        "teams_channel": ctx.cfg.alerts.teams_channel,
        "email_owners": email,
        "owner_emails": owner_emails(ctx) if email else [],
    }
    payload["card"] = adaptive_card(payload)
    return payload


def adaptive_card(p: dict[str, Any]) -> dict[str, Any]:
    """Teams Adaptive Card (schema 1.5). Posted as-is by the flow or the Teams webhook."""
    resolved = p["event"] == "incident.resolved"
    verb = {"incident.opened": "New incident", "incident.worsened": "Incident got worse",
            "incident.resolved": "Resolved"}.get(p["event"], "Update")
    style = "good" if resolved else _STYLE.get(p["severity"], "default")
    badge = "✅" if resolved else _EMOJI.get(p["severity"], "")
    body: list[dict[str, Any]] = [
        {
            "type": "Container",
            "style": style,
            "bleed": True,
            "items": [
                {"type": "TextBlock", "text": f"{badge} {verb} · {p['project_name']}", "size": "Small",
                 "weight": "Bolder", "isSubtle": True, "wrap": True},
                {"type": "TextBlock", "text": p["title"], "size": "Large", "weight": "Bolder", "wrap": True},
            ],
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Severity", "value": p["severity"].capitalize()},
                {"title": "Scope", "value": p["scope"].split(":", 1)[-1]},
                {"title": "Checks", "value": ", ".join(c.replace("_", " ") for c in p["checks"]) or "-"},
                {"title": "Incident", "value": f"#{p['incident_id']}"},
            ],
        },
    ]
    if resolved:
        body.append({"type": "TextBlock", "text": "All checks for this scope are passing again.", "wrap": True})
    else:
        body += [
            {"type": "TextBlock", "text": "What happened", "weight": "Bolder", "spacing": "Medium"},
            {"type": "TextBlock", "text": p["summary"], "wrap": True},
            {"type": "TextBlock", "text": "Likely cause", "weight": "Bolder", "spacing": "Medium"},
            {"type": "TextBlock", "text": p["likely_cause"], "wrap": True},
            {"type": "TextBlock", "text": "Suggested action", "weight": "Bolder", "spacing": "Medium"},
            {"type": "TextBlock", "text": p["action"], "wrap": True},
            {"type": "TextBlock", "isSubtle": True, "size": "Small", "wrap": True, "spacing": "Medium",
             "text": ("AI analysis" if p["analysis_source"] == "ai" else "Rule-based analysis")
             + f" · confidence {p['confidence'] or 'n/a'}"},
        ]
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "msteams": {"width": "Full"},
        "body": body,
        "actions": [{"type": "Action.OpenUrl", "title": "Open in portal", "url": p["portal_url"]}],
    }


def enqueue(ctx: ProjectContext, incident: Incident, event: str, event_row: IncidentEvent) -> Notification:
    payload = build_payload(ctx, incident, event)
    note = Notification(
        project_id=ctx.project.id,
        incident_id=incident.id,
        event=event,
        idempotency_key=f"{ctx.key}:incident-{incident.id}:event-{event_row.id}",
        payload=payload,
        channel=ctx.cfg.alerts.notifier,
        created_at=ctx.now,
    )
    ctx.session.add(note)
    return note


def _muted(ctx: ProjectContext, scope: str) -> Mute | None:
    return ctx.session.scalars(
        select(Mute).where(
            Mute.project_id == ctx.project.id,
            Mute.until > ctx.now,
            or_(Mute.scope == "*", Mute.scope == scope),
        )
    ).first()


def dispatch(ctx: ProjectContext) -> dict[str, int]:
    notifier = ctx.connectors.notifier
    counts = {"sent": 0, "dry_run": 0, "muted": 0, "failed": 0, "retrying": 0}
    pending = ctx.session.scalars(
        select(Notification)
        .where(Notification.project_id == ctx.project.id, Notification.status == "pending")
        .order_by(Notification.id)
    ).all()
    for note in pending:
        if note.event.startswith("incident.") and note.event != "incident.resolved":
            mute = _muted(ctx, note.payload.get("scope", ""))
            if mute is not None:
                note.status, note.last_error = "muted", f"muted by {mute.created_by} until {iso(mute.until)}"
                counts["muted"] += 1
                continue
        if not notifier.configured:
            note.status = "dry_run"
            note.sent_at = ctx.now
            counts["dry_run"] += 1
            log.info("[dry-run] %s %s → %s (no flow URL configured)", ctx.key, note.event, note.payload.get("title"))
            continue
        note.attempts += 1
        try:
            notifier.send(note.payload, note.idempotency_key)
            note.status, note.sent_at, note.last_error = "sent", ctx.now, None
            counts["sent"] += 1
        except Exception as exc:  # noqa: BLE001
            note.last_error = str(exc)[:1000]
            if note.attempts >= MAX_ATTEMPTS:
                note.status = "failed"
                counts["failed"] += 1
            else:
                counts["retrying"] += 1
            log.warning("notify %s failed (attempt %s): %s", note.idempotency_key, note.attempts, exc)
    return counts
