"""The bot keeps learning: a lasting new level becomes normal by itself (quickly when it isn't bad news, after a week
when it is, never for "nothing arrived"), and a person can accept a new normal from an incident."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from test_adf import NOW, adf_ws  # noqa: F401 - adf_ws is a fixture

from alertbot.db import BotCycle, CheckResult, Incident, MetricObservation, Project, session_scope
from alertbot.engine.baselines import Band, adapt_to_new_level, band_for

OLD = Band("row_count", 3, 0, 8, 6, "all_days", -1)


def test_a_new_level_becomes_normal_after_a_day_of_it():
    day = [(70.0, NOW), (60.0, NOW - timedelta(hours=10)), (50.0, NOW - timedelta(hours=20))]
    b = adapt_to_new_level(OLD, day, "row_count")
    assert b is not None and b.basis == "new_level" and b.low <= 55 <= b.high and b.median == 60
    hours = [(70.0, NOW), (60.0, NOW - timedelta(hours=2)), (50.0, NOW - timedelta(hours=4))]
    assert adapt_to_new_level(OLD, hours, "row_count") is None  # a few hours isn't a new normal yet
    mixed = [(70.0, NOW), (4.0, NOW - timedelta(hours=10)), (50.0, NOW - timedelta(hours=20)), (60.0, NOW - timedelta(hours=30))]
    assert adapt_to_new_level(OLD, mixed, "row_count") is None  # the streak is broken by a normal run


def test_bad_news_takes_a_week_and_nothing_arriving_never_becomes_normal():
    normal = Band("row_count", 1000, 850, 1150, 20, "all_days", -1)
    drop = [(300.0, NOW - timedelta(days=d)) for d in range(3)]
    assert adapt_to_new_level(normal, drop, "row_count") is None  # fewer rows for 2 days: still flagged
    week = [(300.0, NOW - timedelta(days=d)) for d in range(8)]
    assert adapt_to_new_level(normal, week, "row_count").median == 300  # a week of it: that's the new reality
    zeros = [(0.0, NOW - timedelta(days=d)) for d in range(10)]
    assert adapt_to_new_level(normal, zeros, "row_count") is None
    empties = Band("null_rate", 0.01, 0, 0.05, 20, "all_days", -1)
    assert adapt_to_new_level(empties, [(0.3, NOW - timedelta(days=d)) for d in range(3)], "null_rate") is None


def test_accepting_a_new_normal_from_the_incident(adf_ws):  # noqa: F811
    from alertbot.api.main import app
    from alertbot.config import load_projects
    from alertbot.engine.cycle import make_context

    cfg = load_projects()["adfp"]
    out = "table:DB.RAW.ORDERS"
    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        for d in range(8, 0, -1):  # a week of 0-5 rows
            s.add(MetricObservation(project_id=p.id, output_id=out, site="pl_sql", metric="row_count", field="", run_ref=f"old{d}",
                                    weekday=(NOW - timedelta(days=d)).weekday(), value=float(d % 5), observed_at=NOW - timedelta(days=d)))
        s.add(MetricObservation(project_id=p.id, output_id=out, site="pl_sql", metric="row_count", field="", run_ref="new1",
                                weekday=NOW.weekday(), value=50.0, observed_at=NOW))
        inc = Incident(project_id=p.id, fingerprint="x", scope=out, title="pl_sql copied unusually many rows", severity="warning",
                       opened_at=NOW, status="open")
        s.add(inc)
        s.flush()
        cyc = BotCycle(project_id=p.id, started_at=NOW, status="ok")
        s.add(cyc)
        s.flush()
        s.add(CheckResult(cycle_id=cyc.id, project_id=p.id, check="row_count", scope=out, status="fail", severity="warning",
                          message="50 rows against 0-5", actual=50, incident_id=inc.id, created_at=NOW))
        inc_id = inc.id
        before = band_for(make_context(s, cfg, NOW), out, "pl_sql", "row_count", "", NOW.weekday(), before=NOW + timedelta(minutes=5))
    assert before is not None and before.high < 50  # still the old normal

    api = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com"})
    r = api.post(f"/api/projects/adfp/incidents/{inc_id}/accept-normal")
    assert r.status_code == 200, r.text
    assert any(e["kind"] == "accepted_normal" for e in r.json()["events"]) and r.json()["status"] == "acknowledged"
    with session_scope() as s:
        after = band_for(make_context(s, cfg, NOW + timedelta(hours=1)), out, "pl_sql", "row_count", "", NOW.weekday(),
                         before=NOW + timedelta(hours=1))
    assert after.basis == "accepted" and after.low <= 50 <= after.high and after.low <= 80 <= after.high  # 50-100 is normal now
    assert api.post(f"/api/projects/adfp/incidents/{inc_id}/accept-normal").status_code == 200  # idempotent enough: just another reset


def test_a_pipeline_renamed_or_deleted_in_azure_leaves_the_list(adf_ws, monkeypatch):  # noqa: F811
    """Azure's own list decides which pipelines exist: one that's gone isn't counted, is named as gone, and its open
    incidents are closed (they could never resolve by themselves)."""
    from alertbot.api.main import app
    from alertbot.config import load_projects
    from alertbot.db import BotState, Run
    from alertbot.engine.adf import close_gone
    from alertbot.engine.cycle import make_context

    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        s.add(BotState(key="inventory:adfp", value={"items": [{"name": "pl_new", "steps": 1}], "at": NOW.isoformat()}))
        s.add(Run(project_id=p.id, trigger_id="t", site="pipeline1", pipeline="Manual", trigger_status="succeeded", run_id="r1",
                  run_status="Succeeded", requested_at=NOW - timedelta(hours=20)))
        s.add(Incident(project_id=p.id, fingerprint="g", scope="site:pipeline1", title="pipeline1 failed", severity="critical",
                       opened_at=NOW - timedelta(hours=20), status="open"))
    from conftest import freeze_now

    freeze_now(monkeypatch, NOW + timedelta(hours=1))  # the history is built at a fixed date
    api = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com"})
    d = api.get("/api/pipelines?days=7").json()
    names = [i["name"] for i in d["items"] if i["project"] == "adfp"]
    assert "pipeline1" not in names and "pl_new" in names
    assert next(s for s in d["sources"] if s["project"] == "adfp")["gone"] == ["pipeline1"]
    with session_scope() as s:
        close_gone(make_context(s, load_projects()["adfp"], NOW), {"pl_new"})
    with session_scope() as s:
        inc = s.query(Incident).filter_by(scope="site:pipeline1").one()
        assert inc.status == "resolved" and "no longer in Azure" in inc.events[-1].message


def test_projects_without_their_own_flow_use_the_default_one(monkeypatch):
    from conftest import _clear_caches

    from alertbot.config import AlertsConfig
    from alertbot.connectors.notifiers import build_notifier

    monkeypatch.setenv("ALERTBOT_DEFAULT_FLOW_URL", "env://PA_SHARED")
    monkeypatch.setenv("PA_SHARED", "https://flow.example/invoke")
    _clear_caches()
    assert build_notifier(AlertsConfig(notifier="power_automate", flow_url="")).configured
    monkeypatch.setenv("ALERTBOT_DEFAULT_FLOW_URL", "")
    _clear_caches()
    assert not build_notifier(AlertsConfig(notifier="power_automate", flow_url="")).configured
    _clear_caches()


def test_preferences_follow_the_person(adf_ws):  # noqa: F811
    from alertbot.api.main import app

    me = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com"})
    assert me.get("/api/me").json()["prefs"] == {}
    me.put("/api/me/prefs", json={"dock": {"global": ["lineage", "home"]}})
    me.put("/api/me/prefs", json={"tour_done_at": "2026-10-14T12:00:00Z", "tour_version": 1})
    prefs = me.get("/api/me").json()["prefs"]
    assert prefs == {"dock": {"global": ["lineage", "home"]}, "tour_done_at": "2026-10-14T12:00:00Z", "tour_version": 1}
    other = TestClient(app, headers={"Authorization": "Bearer dev:other@contoso.com"})
    assert other.get("/api/me").json()["prefs"] == {}  # each person's own
    assert me.put("/api/me/prefs", json={"anything": "else"}).status_code == 200 and "anything" not in me.get("/api/me").json()["prefs"]
