from __future__ import annotations

import pytest
from sqlalchemy import select

from alertbot.db import AgentToolCall, Incident, Notification, session_scope
from alertbot.engine.baselines import robust_band
from alertbot.engine.cycle import run_cycle
from alertbot.sim.scenarios import apply_scenario


def _incidents():
    with session_scope() as s:
        return [(i.id, i.title, i.status, i.severity, sorted(i.checks), i.likely_cause) for i in s.scalars(select(Incident).order_by(Incident.id))]


def _notes():
    with session_scope() as s:
        return [(n.event, n.status) for n in s.scalars(select(Notification).order_by(Notification.id))]


# ── baselines ─────────────────────────────────────────────────────────────────


def test_row_count_band_is_robust_to_one_outlier():
    normal = [1000, 1010, 990, 1005, 995, 1002, 998]
    med, low, high = robust_band([*normal, 120], "row_count")
    assert 990 <= med <= 1005
    assert low > 800 and high < 1200  # one bad run does not blow the band open


def test_null_rate_band_has_absolute_floor():
    _, low, high = robust_band([0.0, 0.0, 0.001, 0.0], "null_rate")
    assert low == 0.0 and high >= 0.02


# ── the doc's story, end to end ───────────────────────────────────────────────


def test_healthy_history_opens_nothing(estate):
    assert _incidents() == []
    assert _notes() == []


def test_story_incomplete_scrape_becomes_one_incident_then_resolves(estate):
    apply_scenario(estate, "incomplete")
    report = run_cycle(estate)
    assert report.status == "ok"
    assert report.failed_checks == 2

    [(inc_id, title, status, severity, checks, cause)] = _incidents()
    assert title == "Site A scrape incomplete"
    assert (status, severity) == ("open", "critical")
    assert checks == ["null_spike", "row_count"]  # two failing checks grouped into one incident
    assert "layout" in cause
    assert _notes() == [("incident.opened", "dry_run")]  # no flow URL → dry-run, not lost

    with session_scope() as s:
        tools = {t.tool for t in s.scalars(select(AgentToolCall).where(AgentToolCall.incident_id == inc_id))}
    assert {"get_activity_runs", "compare_schema", "get_trigger_rows"} <= tools  # investigation is audited

    run_cycle(estate)  # same state again: no duplicate alert
    assert _notes() == [("incident.opened", "dry_run")]

    apply_scenario(estate, "recover")
    run_cycle(estate)
    assert _incidents()[0][2] == "resolved"
    assert _notes()[-1] == ("incident.resolved", "dry_run")


@pytest.mark.parametrize(
    ("scenario", "title", "check", "severity"),
    [
        ("blocked", "Site A returned no data", "empty_site", "critical"),
        ("failed", "Site A scrape failed", "run_failed", "critical"),
        ("stuck", "Site A trigger stuck", "stuck_trigger", "warning"),  # critical only past 3x the limit
        ("slow", "Site A scrape running slow", "run_too_slow", "warning"),
        ("duplicates", "Site A duplicate rows", "duplicates", "warning"),
        ("spike", "Site A row volume unusually high", "row_count", "warning"),
    ],
)
def test_scenarios_produce_the_right_incident(estate, scenario, title, check, severity):
    apply_scenario(estate, scenario)
    run_cycle(estate)
    [(_, got_title, status, got_sev, checks, cause)] = _incidents()
    assert got_title == title
    assert check in checks
    assert got_sev == severity
    assert status == "open" and cause


def test_schema_drift_and_accept(estate):
    from alertbot.engine.checks import accept_schema
    from alertbot.engine.cycle import make_context

    apply_scenario(estate, "schema-drop")
    run_cycle(estate)
    [(_, title, _, severity, checks, _)] = _incidents()
    assert title == "listings table schema changed" and severity == "critical" and checks == ["schema_drift"]

    with session_scope() as s:
        ctx = make_context(s, estate)
        accept_schema(ctx, "listings", ctx.now)
    run_cycle(estate)
    assert _incidents()[0][2] == "resolved"


def test_muted_project_records_but_does_not_send(estate):
    from datetime import timedelta

    from alertbot.db import Mute, Project
    from alertbot.timeutil import utcnow

    with session_scope() as s:
        p = s.scalars(select(Project).where(Project.key == "slipstream")).one()
        s.add(Mute(project_id=p.id, scope="*", until=utcnow() + timedelta(hours=1), created_by="test"))
    apply_scenario(estate, "failed")
    run_cycle(estate)
    assert _incidents()[0][2] == "open"
    assert _notes() == [("incident.opened", "muted")]
