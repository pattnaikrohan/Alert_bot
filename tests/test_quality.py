"""Data quality inside the tables the pipelines load, with a SQLite stand-in for Snowflake (one attached file per schema).

Ten normal days teach each table its normal; then one bad day: fewer rows, empty customer ids, duplicates, negative
amounts, orphan references and a new column. Every check must notice, and the bot must explain it in one incident.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from conftest import freeze_now
from sqlalchemy import select
from test_adf import NOW, PROJECT, adf_ws, cycle, history  # noqa: F401 - adf_ws is a fixture

from alertbot.db import CheckResult, Incident, session_scope

QUALITY = """
quality:
  source:
    type: sqlite
    url: sqlite:///{wh}
  every_hours: 6
  tables:
    - table: DB_PROD.RAW.TERMINAL
      key: [ID]
      required: [CUSTOMER_ID]
      rules:
        - name: amount is positive
          sql: ORDER_AMOUNT > 0
      references:
        - column: BA_ID
          table: DB_PROD.RAW.BA_ID
          ref_column: BA_ID
"""


def _warehouse(tmp_path):
    db = sqlite3.connect(tmp_path / "wh.db.raw")
    db.executescript("""
        CREATE TABLE TERMINAL (ID INTEGER, CUSTOMER_ID TEXT, ORDER_AMOUNT NUMERIC, BA_ID TEXT, PORT VARCHAR(5), LOAD_DATE TIMESTAMP);
        CREATE TABLE BA_ID (BA_ID TEXT, LOAD_DATE TIMESTAMP);
        CREATE TABLE CNTR (CNTR_NO TEXT, LOAD_DATE TIMESTAMP);
    """)
    db.executemany("INSERT INTO BA_ID VALUES (?, ?)", [(f"BA{i}", "2026-09-01 00:00:00") for i in range(50)])
    db.commit()
    return db


def _load_day(db, day, rows=1000, start_id=0, null_ids=0, dup=0, negative=0, orphans=0, skew=0.0, amount_x=1):
    at = day.replace(hour=15, minute=5).isoformat(sep=" ")
    ports = ["SGSIN", "NLRTM", "CNSHA"]
    data = [(start_id + i, None if i < null_ids else f"C{i % 300}", -5 if i < negative else (10 + i % 7) * amount_x,
             "NOPE" if i < orphans else f"BA{i % 50}", "SGSIN" if i < rows * skew else ports[i % 3], at) for i in range(rows)]
    data += data[:dup]
    db.executemany("INSERT INTO TERMINAL VALUES (?, ?, ?, ?, ?, ?)", data)
    db.executemany("INSERT INTO CNTR VALUES (?, ?)", [(f"K{i}", at) for i in range(100)])
    db.commit()


@pytest.fixture
def dq(adf_ws, tmp_path):  # noqa: F811
    (tmp_path / "config" / "projects" / "adfp.yaml").write_text(PROJECT + QUALITY.format(wh=(tmp_path / "wh.db").as_posix()), encoding="utf-8")
    from conftest import _clear_caches

    from alertbot.engine import quality

    _clear_caches()
    quality._engine.cache_clear()
    db = _warehouse(tmp_path)
    yield adf_ws, db
    db.close()
    quality._engine.cache_clear()


def _check(at, force=True):
    from alertbot.config import load_projects
    from alertbot.engine.cycle import make_context
    from alertbot.engine.quality import run_quality

    with session_scope() as s:
        return run_quality(make_context(s, load_projects()["adfp"], at), force=force)


def _normal_history(db, days=10):
    for d in range(days, 0, -1):
        day = NOW - timedelta(days=d)
        _load_day(db, day, start_id=d * 10_000)
        _check(day.replace(hour=16, minute=0))


def test_tables_are_found_from_the_pipelines_and_learn_their_normal(dq):
    fake, db = dq
    history(fake)
    _normal_history(db, days=6)
    from alertbot.config import load_projects
    from alertbot.engine.cycle import make_context
    from alertbot.engine.quality import quality_targets

    with session_scope() as s:
        found = {t.table: p for t, p in quality_targets(make_context(s, load_projects()["adfp"], NOW))}
    # PROD tables the copy steps write (DEV ones are left out), plus the configured one
    assert found == {"DB_PROD.RAW.TERMINAL": "DEV-SG-TERMINAL-CSV", "DB_PROD.RAW.BA_ID": "DEV-EMPTY", "DB_PROD.RAW.CNTR": "DEV-EMPTY"}

    _load_day(db, NOW, start_id=1)
    out = _check(NOW)
    terminal = {o.check: o for o in out if o.scope == "data:DB_PROD.RAW.TERMINAL"}
    assert {o.status for o in out} <= {"pass", "skip"}, [o.message for o in out if o.failed]
    assert terminal["row_count"].status == "pass" and terminal["row_count"].actual == 1000
    assert terminal["duplicates"].status == "pass" and terminal["business_rule"].status == "pass"
    assert terminal["referential_integrity"].status == "pass" and terminal["freshness"].status == "pass"


def test_a_bad_day_is_caught_by_every_check_and_explained(dq):
    fake, db = dq
    history(fake)
    _normal_history(db)
    # the bad day: 2k → 200 rows; 20% without a customer; 10 duplicates; 5 negative amounts; 3 unknown BA ids; a new column
    _load_day(db, NOW, rows=200, start_id=1, null_ids=40, dup=10, negative=5, orphans=3)
    db.execute("ALTER TABLE TERMINAL ADD COLUMN SOURCE_FILE TEXT")
    db.commit()
    cycle()

    with session_scope() as s:
        results = {r.check: r for r in s.scalars(select(CheckResult).where(CheckResult.scope == "data:DB_PROD.RAW.TERMINAL"))
                   if r.status == "fail"}
        assert set(results) == {"row_count", "null_spike", "duplicates", "value_range", "business_rule", "referential_integrity",
                                "schema_drift"}
        assert "CUSTOMER_ID is empty in 23.8% of rows" in results["null_spike"].message  # 50 of 210 (the duplicates too)
        assert results["row_count"].details["direction"] == "low" and results["row_count"].severity == "critical"
        assert "added SOURCE_FILE" in results["schema_drift"].message
        assert "rule 'amount is positive'" in results["business_rule"].message

        inc = s.scalars(select(Incident).where(Incident.scope == "data:DB_PROD.RAW.TERMINAL")).one()
        assert inc.title == "DB_PROD.RAW.TERMINAL has unusually few rows today"
        assert inc.severity == "critical"
        # the email's format: expected / actual / deviation, then the investigation and the likely source
        assert "Expected: ~1,000 rows" in inc.summary and "Actual: 210 rows" in inc.summary and "Deviation:" in inc.summary
        assert "Initial investigation:" in inc.summary and "copied" in inc.summary
        assert "Schema change detected." in inc.summary
        assert inc.likely_cause and inc.suggested_action


def test_stale_data_and_a_missing_warehouse(dq, monkeypatch):
    fake, db = dq
    history(fake)
    _normal_history(db, days=6)
    # nothing new for two days
    out = _check(NOW + timedelta(days=2))
    fresh = next(o for o in out if o.check == "freshness" and o.scope == "data:DB_PROD.RAW.TERMINAL")
    assert fresh.failed and "h old" in fresh.message

    from alertbot.config import load_projects
    from alertbot.engine.quality import get_status

    with session_scope() as s:
        status = get_status(s, "adfp")
    assert status["ok"] and status["checked"] == 3

    # the warehouse goes away (or the sign-in expired): one clear status, no per-table noise, and no retry every cycle
    from alertbot.engine import quality

    calls = []

    def down(_dsn):  # noqa: ANN001
        calls.append(1)
        raise RuntimeError("SSO sign-in required")

    down.cache_clear = lambda: None
    monkeypatch.setattr(quality, "_engine", down)
    assert _check(NOW + timedelta(days=2, hours=1)) == []
    with session_scope() as s:
        status = get_status(s, "adfp")
    assert status["unavailable"] and "can't connect" in status["message"]
    assert _check(NOW + timedelta(days=2, hours=1, minutes=10), force=False) == [] and len(calls) == 1  # resting for 30 min
    assert _check(NOW + timedelta(days=2, hours=7), force=False) == [] and len(calls) == 2  # due again: tries again
    assert load_projects()["adfp"].quality is not None


def test_quality_api_and_health_score(dq):
    fake, db = dq
    history(fake)
    _normal_history(db)
    _load_day(db, NOW, rows=1000, start_id=1, null_ids=200)
    cycle()
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    c = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com", "X-AlertBot": "1"})
    q = c.get("/api/projects/adfp/quality").json()
    t = next(x for x in q["tables"] if x["name"] == "TERMINAL")
    assert t["status"] == "fail" and t["nulls"][0]["column"] == "CUSTOMER_ID" and t["rows_today"] == 1000
    assert any(ch["label"] == "Business rules: amount is positive" for ch in t["checks"])
    assert q["summary"]["failing"] == 1 and q["summary"]["score"] < 100
    m = c.get("/api/projects/adfp/management").json()
    assert m["health"]["score"] is not None and m["health"]["parts"]["quality"] == q["summary"]["score"]
    assert any("data-quality" in r for r in m["status"]["reasons"])
    assert next(x for x in m["tables"] if x["name"] == "TERMINAL")["quality"]["status"] == "fail"
    # on-demand check: owners only
    other = TestClient(app, headers={"Authorization": "Bearer dev:other@contoso.com", "X-AlertBot": "1"})
    assert other.post("/api/projects/adfp/quality/check").status_code == 404
    r = c.post("/api/projects/adfp/quality/check")
    assert r.status_code == 200, r.text


def test_morning_report_is_built_and_sent_once_a_day(dq, monkeypatch):
    fake, db = dq
    history(fake)
    _normal_history(db)
    _load_day(db, NOW, rows=200, start_id=1)
    cycle()
    freeze_now(monkeypatch, NOW + timedelta(hours=1))  # the history is built at a fixed date
    from alertbot.config import load_projects
    from alertbot.db import Notification
    from alertbot.engine.report import due_reports, send_report

    cfg = load_projects()["adfp"]
    assert cfg.alerts.morning_report == "08:00"
    r = send_report(cfg, NOW, use_ai=False)
    assert r is not None
    text = r["text"]
    assert "Data Health Score:" in text and "Pipelines (last 24 h):" in text and "quality-checked" in text
    assert r["issues"]["critical"] >= 1 and "DB_PROD.RAW.TERMINAL has unusually few rows today" in text
    assert r["tables"]["checked"] == 3 and r["tables"]["anomalies"] >= 1
    assert send_report(cfg, NOW + timedelta(hours=1), use_ai=False) is None  # once a day
    with session_scope() as s:
        notes = s.scalars(select(Notification).where(Notification.event == "report.morning")).all()
        assert len(notes) == 1 and notes[0].status == "dry_run"  # no flow URL in tests
        card = notes[0].payload["card"]
        assert card["body"][0]["items"][1]["text"].startswith("Data Health Score:")
    # the worker sends tomorrow's after 08:00 local, not before
    tomorrow = NOW.replace(hour=0, minute=0) + timedelta(days=1)
    assert due_reports(tomorrow.replace(hour=7, minute=59)) == []
    assert due_reports(tomorrow.replace(hour=8, minute=1)) == ["adfp"]
    assert due_reports(tomorrow.replace(hour=9)) == []


def test_is_todays_data_ready_counts_data_quality(dq, monkeypatch):
    fake, db = dq
    history(fake)
    _normal_history(db)
    _load_day(db, NOW, rows=1000, start_id=1, null_ids=300)
    cycle()
    freeze_now(monkeypatch, NOW + timedelta(hours=1))  # the history is built at a fixed date
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    c = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com", "X-AlertBot": "1"})
    a = c.post("/api/ask", json={"question": "Is today's data ready?"}).json()
    assert "TERMINAL: PROD ❌" in a["answer"] and "data checks failing (empty values)" in a["answer"]
    assert "Not fully ready" in a["answer"]
    r = c.get("/api/projects/adfp/report").json()
    assert r["health"]["score"] is not None and r["tables"]["failing_quality"] == 1


def test_distribution_changes_are_caught(dq):
    fake, db = dq
    history(fake)
    _normal_history(db)
    _load_day(db, NOW, rows=1000, start_id=1, skew=0.8, amount_x=10)  # 80%+ from one port; amounts 10x
    out = _check(NOW)
    shift = next(o for o in out if o.check == "distribution_shift" and o.scope == "data:DB_PROD.RAW.TERMINAL")
    assert shift.failed, shift.message
    fields = {f["field"]: f for f in shift.details["fields"]}
    assert set(fields) == {"ORDER_AMOUNT", "PORT"}  # ids are left alone
    assert "PORT: its most common value is now 87% of rows (normally 33%)" in shift.message
    assert "SGSIN" not in shift.message and "SGSIN" not in str(shift.details)  # the value itself never leaves the warehouse


def test_a_running_pipeline_predicted_to_finish_late(dq):
    fake, db = dq
    history(fake)
    rid = fake.add("DEV-SG-TERMINAL-CSV", NOW - timedelta(minutes=8), minutes=5, status="InProgress", rows=1000)
    fake.acts[rid][0]["status"] = "Succeeded"  # 1 of 2 steps done after 8 minutes; it usually takes ~5
    cycle()
    with session_scope() as s:
        r = s.scalars(select(CheckResult).where(CheckResult.check == "sla_risk", CheckResult.status == "fail")).one()
        assert "1 of 2 steps finished after 8 min" in r.message and "likely to miss its usual finish" in r.message
        inc = s.scalars(select(Incident).where(Incident.scope == "site:DEV-SG-TERMINAL-CSV")).one()
        assert inc.title == "DEV-SG-TERMINAL-CSV is likely to finish late"


def test_remediation_is_read_only_and_schema_can_be_accepted(dq):
    fake, db = dq
    history(fake)
    _normal_history(db)
    _load_day(db, NOW, start_id=1)
    db.execute("ALTER TABLE TERMINAL ADD COLUMN SOURCE_FILE TEXT")
    db.commit()
    cycle()
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    c = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com", "X-AlertBot": "1"})
    with session_scope() as s:
        inc = s.scalars(select(Incident).where(Incident.scope == "data:DB_PROD.RAW.TERMINAL")).one()
        inc_id = inc.id
    assert inc.title == "DB_PROD.RAW.TERMINAL schema changed"
    plan = c.get(f"/api/projects/adfp/incidents/{inc_id}").json()["remediation"]
    assert plan["mode"] == "read_only" and plan["recommended"] == "accept_schema"
    assert {o["key"] for o in plan["options"]} >= {"accept_schema", "notify", "ignore"}
    assert all(not o["changes_factory"] or o["href"] for o in plan["options"])  # factory changes only as links

    assert c.post("/api/projects/adfp/quality/DB_PROD.RAW.TERMINAL/accept-schema").status_code == 200
    out = _check(NOW + timedelta(minutes=30))
    drift = next(o for o in out if o.check == "schema_drift" and o.scope == "data:DB_PROD.RAW.TERMINAL")
    assert drift.status == "pass"
