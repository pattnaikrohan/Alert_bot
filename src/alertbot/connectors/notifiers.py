"""Notifiers: how an alert leaves the bot.

The flow URL is a secret reference resolved at send time. When it resolves to
nothing, the notifier reports `configured = False` and the dispatcher stores
the alert as a dry run. Nothing is lost; the portal shows exactly what would
have been posted.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..config import AlertsConfig
from ..secrets import resolve_secret

log = logging.getLogger(__name__)


class NotifyError(RuntimeError):
    pass


class _HttpNotifier:
    channel = "http"

    def __init__(self, alerts: AlertsConfig, timeout: float = 15.0, attempts: int = 3):
        from ..settings import get_settings

        self._ref = alerts.flow_url or get_settings().default_flow_url  # a project without its own flow uses the default one
        self._timeout = timeout
        self._attempts = attempts

    @property
    def url(self) -> str | None:
        return resolve_secret(self._ref)

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _body(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def send(self, payload: dict[str, Any], idempotency_key: str) -> None:
        url = self.url
        if not url:
            raise NotifyError("flow URL is not configured")
        headers = {"Content-Type": "application/json", "x-alertbot-idempotency-key": idempotency_key}
        last: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                r = httpx.post(url, json=self._body(payload), headers=headers, timeout=self._timeout)
                if r.status_code < 300:
                    return
                if r.status_code < 500 and r.status_code != 429:
                    raise NotifyError(f"HTTP {r.status_code}: {r.text[:300]}")  # not retryable
                last = NotifyError(f"HTTP {r.status_code}")
            except httpx.HTTPError as exc:
                last = exc
            if attempt < self._attempts:
                time.sleep(min(2**attempt, 10))
        raise NotifyError(f"gave up after {self._attempts} attempts: {last}")


class PowerAutomateNotifier(_HttpNotifier):
    """Posts the full alert JSON to a flow starting with "When an HTTP request is received".

    The payload carries a ready-made Adaptive Card under `card`, so the flow can
    post it to Teams as-is, or build its own from the plain fields.
    """

    channel = "power_automate"


class TeamsWorkflowNotifier(_HttpNotifier):
    """Teams "Workflows" webhook (no premium licence). Teams-only: posts the card."""

    channel = "teams_workflow"

    def _body(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "message",
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": payload["card"]}
            ],
        }


class NullNotifier:
    channel = "none"
    configured = False

    def send(self, payload: dict[str, Any], idempotency_key: str) -> None:  # pragma: no cover
        raise NotifyError("alerts are disabled for this project")


def build_notifier(alerts: AlertsConfig):  # noqa: ANN201
    if alerts.notifier == "power_automate":
        return PowerAutomateNotifier(alerts)
    if alerts.notifier == "teams_workflow":
        return TeamsWorkflowNotifier(alerts)
    return NullNotifier()
