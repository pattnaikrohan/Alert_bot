from __future__ import annotations

import pytest
from sqlalchemy import select

from alertbot.db import Notification, session_scope
from alertbot.engine.cycle import run_cycle
from alertbot.sim.scenarios import apply_scenario


class _Capture:
    def __init__(self):
        self.calls = []

    def __call__(self, url, json, headers, timeout):  # noqa: A002 - mirrors httpx.post
        self.calls.append({"url": url, "json": json, "headers": headers})

        class R:
            status_code = 202
            text = ""

        return R()


def test_flow_url_slot_switches_from_dry_run_to_live(estate, monkeypatch):
    capture = _Capture()
    monkeypatch.setattr("alertbot.connectors.notifiers.httpx.post", capture)
    monkeypatch.setenv("PA_SLIPSTREAM_URL", "https://example.invalid/flow/trigger?sig=secret")

    apply_scenario(estate, "incomplete")
    run_cycle(estate)

    [call] = capture.calls
    body = call["json"]
    assert call["url"].startswith("https://example.invalid/flow")
    assert call["headers"]["x-alertbot-idempotency-key"].startswith("slipstream:incident-")
    # The doc's payload fields, plus the extras the flow needs to route generically.
    for field in ("project", "incident_id", "severity", "title", "summary", "likely_cause", "action", "portal_url",
                  "event", "schema_version", "owner_emails", "card"):
        assert field in body, field
    assert body["event"] == "incident.opened" and body["email_owners"] is True
    assert body["owner_emails"] == ["rohan@contoso.com"]
    assert body["card"]["type"] == "AdaptiveCard"
    with session_scope() as s:
        assert [n.status for n in s.scalars(select(Notification))] == ["sent"]


def test_teams_workflow_body_wraps_the_card():
    from alertbot.config import AlertsConfig
    from alertbot.connectors.notifiers import TeamsWorkflowNotifier

    body = TeamsWorkflowNotifier(AlertsConfig(notifier="teams_workflow", flow_url="x"))._body({"card": {"type": "AdaptiveCard"}})
    assert body["type"] == "message"
    assert body["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"


def test_failed_send_stays_pending_for_retry(estate, monkeypatch):
    def boom(*a, **k):
        import httpx

        raise httpx.ConnectError("down")

    monkeypatch.setattr("alertbot.connectors.notifiers.httpx.post", boom)
    monkeypatch.setattr("alertbot.connectors.notifiers.time.sleep", lambda s: None)
    monkeypatch.setenv("PA_SLIPSTREAM_URL", "https://example.invalid/flow")
    apply_scenario(estate, "failed")
    run_cycle(estate)
    with session_scope() as s:
        [n] = s.scalars(select(Notification)).all()
        assert (n.status, n.attempts) == ("pending", 1) and "down" in n.last_error


# ── API access control ────────────────────────────────────────────────────────


@pytest.fixture
def client(estate):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    return TestClient(app)


def H(email):
    return {"Authorization": f"Bearer dev:{email}"}


def test_api_requires_auth(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer dev:nobody@contoso.com"}).status_code == 401


def test_users_only_see_their_projects(client):
    assert [p["key"] for p in client.get("/api/me", headers=H("priya.sharma@contoso.com")).json()["projects"]] == ["slipstream"]
    assert client.get("/api/me", headers=H("alex.morgan@contoso.com")).json()["projects"] == []
    # Daniel is in CSDW only: Slipstream must look like it doesn't exist.
    assert client.get("/api/projects/slipstream/overview", headers=H("daniel.okafor@contoso.com")).status_code == 404


def test_incident_ids_cannot_cross_projects(client, estate):
    apply_scenario(estate, "failed")
    run_cycle(estate)
    r = client.get("/api/projects/slipstream/incidents", headers=H("priya.sharma@contoso.com")).json()
    assert r and client.get(f"/api/projects/slipstream/incidents/{r[0]['id']}", headers=H("priya.sharma@contoso.com")).status_code == 200
    assert client.get(f"/api/projects/csdw/incidents/{r[0]['id']}", headers=H("priya.sharma@contoso.com")).status_code == 404


def test_bot_feed_is_scoped_to_the_user(client, estate):
    apply_scenario(estate, "incomplete")
    run_cycle(estate)
    feed = client.get("/api/feed", headers=H("priya.sharma@contoso.com")).json()
    assert feed["mood"] == "critical"
    assert [(i["project"], i["title"]) for i in feed["active"]] == [("slipstream", "Site A scrape incomplete")]
    # Daniel is in CSDW only: the bot must not tell him about Slipstream.
    other = client.get("/api/feed", headers=H("daniel.okafor@contoso.com")).json()
    assert other["active"] == [] and other["projects"] == [] and other["mood"] == "ok"


def test_owner_only_actions(client):
    body = {"hours": 1, "reason": "maintenance"}
    assert client.post("/api/projects/slipstream/mutes", json=body, headers=H("priya.sharma@contoso.com")).status_code == 403
    assert client.post("/api/projects/slipstream/mutes", json=body, headers=H("rohan@contoso.com")).status_code == 201


def test_ask_the_bot_answers_from_the_users_own_projects(client, estate):
    apply_scenario(estate, "failed", site="Site B")
    run_cycle(estate)
    ask = lambda who, q: client.post("/api/ask", json={"question": q}, headers=H(who)).json()  # noqa: E731
    why = ask("priya.sharma@contoso.com", "Why did Site B fail?")
    assert why["answer"].startswith("Site B scrape failed") and "timed out" in why["answer"]
    assert why["links"][0]["to"].startswith("/p/slipstream/i/")
    wrong = ask("priya.sharma@contoso.com", "what's wrong right now?")
    assert "1 open incident" in wrong["answer"] and "Site B" in wrong["answer"]
    assert ask("priya.sharma@contoso.com", "any failed runs this week?")["answer"].startswith("1 of ")
    # Daniel is in CSDW only: asking about a Slipstream site tells him nothing about it.
    other = ask("daniel.okafor@contoso.com", "Why did Site B fail?")
    assert "Site B scrape" not in other["answer"]
    assert "not on a project" in ask("alex.morgan@contoso.com", "what's wrong?")["answer"]


def test_summary_and_check_now(client, estate):
    s = client.get("/api/summary", headers=H("priya.sharma@contoso.com")).json()
    assert "Slipstream" in s["answer"] and "CSDW" not in s["answer"]
    r = client.post("/api/check-now", headers=H("priya.sharma@contoso.com")).json()
    assert [p["key"] for p in r["projects"]] == ["slipstream"] and r["projects"][0]["status"] == "ok"
    assert client.post("/api/check-now?project=csdw", headers=H("priya.sharma@contoso.com")).status_code == 404


def test_config_view_never_leaks_the_flow_url(client, monkeypatch):
    monkeypatch.setenv("PA_SLIPSTREAM_URL", "https://example.invalid/flow?sig=TOPSECRET")
    text = client.get("/api/projects/slipstream/config", headers=H("rohan@contoso.com")).text
    assert "TOPSECRET" not in text and "env://PA_SLIPSTREAM_URL" in text


# ── config validation ─────────────────────────────────────────────────────────


def test_unknown_check_is_rejected():
    from alertbot.config import ProjectConfig

    with pytest.raises(ValueError, match="unknown checks"):
        ProjectConfig.model_validate({
            "project": "x", "display_name": "X",
            "sources": [{"id": "main", "type": "sqlite", "url": "sqlite://"}],
            "pipeline": {"type": "fake_adf", "source": "main"},
            "trigger_table": {"source": "main", "table": "t"},
            "outputs": [{"id": "o", "source": "main", "table": "o", "key": ["k"], "group_by": "s", "loaded_at": "l"}],
            "checks": {"row_cuont": {}},
        })


def test_management_overview_works_for_scrape_projects_too(client):
    m = client.get("/api/projects/slipstream/management?days=7", headers=H("priya.sharma@contoso.com")).json()
    assert m["kpis"]["runs"] > 0 and m["tables"] == [] and len(m["daily"]) == 7
