"""Feeds projects (CSDW Risk Monitor style): producers write straight into tables on a schedule.

A local SQLite file stands in for Snowflake: `<db>.raw_riskmonitor` is attached as schema RAW_RISKMONITOR.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from alertbot.db import Incident, Notification, Run, session_scope
from alertbot.engine.cycle import backfill, run_cycle

NOW = datetime(2026, 9, 30, 13, 30)  # a Wednesday, 90 min after the 12:00 UTC run
SLOTS = (0, 6, 12, 18)

PROJECT = """
project: riskmon
display_name: Risk Monitor
kind: feeds
timezone: UTC
sources:
  - id: sf
    type: sqlite
    url: sqlite:///{db}
feeds:
  - id: news_api
    name: NEWS API
    source: sf
    table: RAW_RISKMONITOR.NEWS_API
    timestamp: INSERT_DATETIME
    filter: {{ column: URL, exclude: ["%wkwebster%"] }}
    key: [URL]
    required_fields: [TITLE]
    schedule: "0 0,6,12,18 * * *"
    grace_minutes: 60
    producer: {{ type: azure_function, name: prod-azf-newsapi-de }}
  - id: webster
    name: Webster
    source: sf
    table: RAW_RISKMONITOR.NEWS_API
    timestamp: INSERT_DATETIME
    filter: {{ column: URL, include: ["%wkwebster%"] }}
    key: [URL]
    schedule: "0 0,6,12,18 * * *"
    grace_minutes: 60
    producer: {{ type: azure_function, name: prod-azf-wkwebster }}
  - id: cyclone
    name: Tropical cyclone
    source: sf
    table: RAW_RISKMONITOR.CYCLONE_POSITIONS
    timestamp: DATE_TIME_IN_UTC
    schedule: "0 0,6,12,18 * * *"
    event_driven: true
    stale_after_hours: 168
    producer: {{ type: azure_function, name: prod-azf-tropicalcyclone }}
alerts:
  notifier: power_automate
  flow_url: env://PA_RISKMON_URL
"""

MEMBERS = """
users:
  - email: chinmoy@contoso.com
    name: Chinmoy
    projects: { riskmon: owner }
  - email: other@contoso.com
    name: Other
    projects: {}
"""


def _slots(days: int):
    t = (NOW - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    while t <= NOW:
        if t.hour in SLOTS and t.minute == 0:
            yield t
        t += timedelta(hours=1)


class Estate:
    """Writes NEWS_API / CYCLONE_POSITIONS rows the way the Function Apps do."""

    def __init__(self, db: str):
        self.path = f"{db}.raw_riskmonitor"
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE NEWS_API (URL TEXT, TITLE TEXT, INSERT_DATETIME TEXT)")
            c.execute("CREATE TABLE CYCLONE_POSITIONS (STORM TEXT, LAT REAL, DATE_TIME_IN_UTC TEXT)")
        self.n = 0

    def write(self, slot: datetime, news: int = 50, webster: int = 10, empty_titles: int = 0) -> None:
        rows = []
        for i in range(news):
            self.n += 1
            at = slot + timedelta(minutes=3 + i % 7)
            rows.append((f"https://news.example/{self.n}", None if i < empty_titles else f"t{self.n}", f"{at:%Y-%m-%d %H:%M:%S}"))
        for i in range(webster):
            self.n += 1
            rows.append((f"https://wkwebster.example/{self.n}", f"w{self.n}", f"{slot + timedelta(minutes=5 + i % 3):%Y-%m-%d %H:%M:%S}"))
        with sqlite3.connect(self.path) as c:
            c.executemany("INSERT INTO NEWS_API VALUES (?, ?, ?)", rows)

    def cyclone(self, at: datetime) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO CYCLONE_POSITIONS VALUES ('KOINU', 21.5, ?)", (f"{at:%Y-%m-%d %H:%M:%S}",))

    def history(self, days: int = 21, skip: set[tuple[datetime, str]] | None = None, **last) -> None:  # noqa: ANN003
        skip = skip or set()
        for slot in _slots(days):
            kw = dict(news=48 + slot.day % 5, webster=9 + slot.hour % 3)
            if slot == max(_slots(1)):
                kw.update(last)
            if (slot, "webster") in skip:
                kw["webster"] = 0
            self.write(slot, **kw)


@pytest.fixture
def feeds_ws(tmp_path, monkeypatch):
    from conftest import _clear_caches

    cfg = tmp_path / "config"
    (cfg / "projects").mkdir(parents=True)
    db = (tmp_path / "riskmon.db").as_posix()
    (cfg / "projects" / "riskmon.yaml").write_text(PROJECT.format(db=db), encoding="utf-8")
    (cfg / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALERTBOT_DB_URL", f"sqlite:///{(tmp_path / 'alertbot.db').as_posix()}")
    monkeypatch.setenv("ALERTBOT_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("ALERTBOT_AUTH_MODE", "dev")
    monkeypatch.setenv("PA_RISKMON_URL", "")
    _clear_caches()

    from alertbot.db import create_all
    from alertbot.seed import seed

    create_all()
    with session_scope() as s:
        seed(s)
    estate = Estate(db)
    estate.cyclone(NOW - timedelta(days=3))
    yield estate
    from alertbot.db.session import get_engine

    get_engine().dispose()
    _clear_caches()


def _cycle():
    from alertbot.config import load_projects

    cfg = load_projects()["riskmon"]
    backfill(cfg, days=22, now=NOW - timedelta(hours=3))
    report = run_cycle(cfg, now=NOW)
    assert report.status == "ok", report.error
    return report


def _incidents():
    with session_scope() as s:
        return {i.title: i for i in s.scalars(select(Incident))}


def test_healthy_feeds_open_nothing(feeds_ws):
    feeds_ws.history()
    report = _cycle()
    assert report.runs > 0
    assert _incidents() == {}
    with session_scope() as s:
        runs = {r.trigger_id: r for r in s.scalars(select(Run))}
    latest_news = runs[f"news_api@{datetime(2026, 9, 30, 12).isoformat()}"]
    # the URL filter keeps Webster's rows out of NEWS API's count
    assert latest_news.run_status == "Succeeded" and 48 <= latest_news.rows_loaded <= 52
    assert runs[f"webster@{datetime(2026, 9, 30, 12).isoformat()}"].rows_loaded in (9, 10, 11)
    # event-driven feed: no rows this run is normal, not a failure
    assert runs[f"cyclone@{datetime(2026, 9, 30, 12).isoformat()}"].run_status == "NoData"


def test_missed_runs_open_one_critical_incident_with_a_cause(feeds_ws):
    feeds_ws.history(skip={(datetime(2026, 9, 30, 6), "webster"), (datetime(2026, 9, 30, 12), "webster")})
    _cycle()
    incidents = _incidents()
    assert list(incidents) == ["Webster missed a scheduled run"]
    inc = incidents["Webster missed a scheduled run"]
    assert inc.severity == "critical" and "missed_run" in inc.checks
    assert "prod-azf-wkwebster" in inc.likely_cause and "Monitor" in inc.suggested_action
    with session_scope() as s:
        assert [(n.event, n.status) for n in s.scalars(select(Notification))] == [("incident.opened", "dry_run")]


def test_low_volume_run_is_flagged_against_the_learned_baseline(feeds_ws):
    feeds_ws.history(news=6)
    _cycle()
    incidents = _incidents()
    assert "NEWS API volume unusually low" in incidents, list(incidents)
    assert "row_count" in incidents["NEWS API volume unusually low"].checks


def test_empty_required_fields_are_flagged(feeds_ws):
    feeds_ws.history(empty_titles=30)
    _cycle()
    incidents = _incidents()
    assert "NEWS API rows arriving with empty fields" in incidents, list(incidents)


def test_late_rows_resolve_the_incident(feeds_ws):
    feeds_ws.history(skip={(datetime(2026, 9, 30, 12), "webster")})
    _cycle()
    assert "Webster missed a scheduled run" in _incidents()
    feeds_ws.write(datetime(2026, 9, 30, 12, 50), news=0, webster=10)  # the function caught up late
    from alertbot.config import load_projects

    assert run_cycle(load_projects()["riskmon"], now=NOW + timedelta(minutes=15)).status == "ok"
    assert _incidents()["Webster missed a scheduled run"].status == "resolved"


def test_stale_event_driven_feed_is_flagged_after_its_limit(feeds_ws):
    feeds_ws.history()
    with sqlite3.connect(feeds_ws.path) as c:
        c.execute("DELETE FROM CYCLONE_POSITIONS")
    feeds_ws.cyclone(NOW - timedelta(days=9))
    _cycle()
    assert "Tropical cyclone has stopped writing" in _incidents()


def test_portal_tables_and_config_for_a_feeds_project(feeds_ws):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    feeds_ws.history()
    _cycle()
    c = TestClient(app)
    h = {"Authorization": "Bearer dev:chinmoy@contoso.com"}
    tables = c.get("/api/projects/riskmon/tables", headers=h).json()
    news = next(t for t in tables if t["table"] == "RAW_RISKMONITOR.NEWS_API")
    assert {s["site"] for s in news["sites"]} == {"NEWS API", "Webster"}
    assert {col["name"] for col in news["schema"]["live"]} == {"URL", "TITLE", "INSERT_DATETIME"}
    assert c.post("/api/projects/riskmon/tables/raw_riskmonitor.news_api/accept-schema", headers=h).status_code == 200
    cfg = c.get("/api/projects/riskmon/config", headers=h).json()
    assert cfg["kind"] == "feeds" and cfg["pipeline"] is None and len(cfg["feeds"]) == 3
    assert c.get("/api/projects/riskmon/overview", headers=h).status_code == 200
    # someone who is not a member cannot see the project at all
    assert c.get("/api/projects/riskmon/overview", headers={"Authorization": "Bearer dev:other@contoso.com"}).status_code == 404


# ── Microsoft sign-in with Authenticator ──────────────────────────────────────


@pytest.mark.parametrize(("amr", "ok"), [(["pwd"], False), ([], False), (["pwd", "mfa"], True), (["ngcmfa"], True)])
def test_entra_tokens_without_a_second_factor_are_rejected(feeds_ws, monkeypatch, amr, ok):
    from fastapi.testclient import TestClient

    from alertbot import settings
    from alertbot.api import auth
    from alertbot.api.main import app

    monkeypatch.setenv("ALERTBOT_AUTH_MODE", "entra")
    settings.get_settings.cache_clear()
    monkeypatch.setattr(auth, "_entra_claims", lambda token: {
        "oid": "11111111-2222", "preferred_username": "chinmoy@contoso.com", "name": "Chinmoy", "amr": amr})
    r = TestClient(app).get("/api/me", headers={"Authorization": "Bearer x.y.z"})
    if ok:
        assert r.status_code == 200
        assert [p["key"] for p in r.json()["projects"]] == ["riskmon"]  # linked to the pre-added membership by email
    else:
        assert r.status_code == 401 and r.json()["detail"].startswith("mfa_required")
