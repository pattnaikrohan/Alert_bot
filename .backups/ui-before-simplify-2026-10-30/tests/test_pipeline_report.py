"""The pipeline report: runs per day per pipeline, today's pace against the same time on earlier days, overdue
pipelines, rows per table per day, errors grouped by what went wrong, insights that point at the right thing, and only
the projects a person belongs to. Runs are made up; nothing connects anywhere."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from test_adf import adf_ws  # noqa: F401 - fixture

from alertbot.db import MetricObservation, Project, Run, UserProject, session_scope

NOW = datetime(2026, 10, 22, 14, 30)  # UTC, the tests' timezone
ERR = "Operation on target Copy CSV failed: ErrorCode=DelimitedTextMoreColumnsThanDefined,'Message=Error found when processing 'Csv/Tsv Format Text' source 'a.csv' with row number 7"


def _seed(project_id: int) -> None:
    with session_scope() as s:
        n = 0

        def run(site, at, status="Succeeded", error=None, rows=None):
            nonlocal n
            n += 1
            s.add(Run(project_id=project_id, trigger_id=f"t{n}", site=site, trigger_status="succeeded" if status == "Succeeded" else "failed",
                      run_id=f"r{n}", pipeline=f"ScheduleTrigger · tr_{site}", run_status=status, requested_at=at, started_at=at,
                      ended_at=at + timedelta(minutes=3), duration_s=180, error=error))
            if rows is not None:
                s.add(MetricObservation(project_id=project_id, output_id="table:DB.RAW.POSITIONS", site=site, metric="row_count", field="",
                                        run_ref=f"r{n}", weekday=at.weekday(), value=rows, observed_at=at + timedelta(minutes=3)))

        # hourly, 8 days back; today it stopped after 09:00 (now is 14:30): overdue
        t = (NOW - timedelta(days=8)).replace(minute=0)
        while t <= NOW.replace(hour=9, minute=0):
            run("pl_positions", t, rows=1000)
            t += timedelta(hours=1)
        # every 6 h, every other run failing with the same known error
        t = (NOW - timedelta(days=8)).replace(hour=0, minute=10)
        i = 0
        while t <= NOW:
            run("pl_berths", t, "Failed" if i % 2 else "Succeeded", ERR if i % 2 else None)
            t += timedelta(hours=6)
            i += 1


def test_report_counts_pace_overdue_volume_and_errors(adf_ws):  # noqa: F811
    from alertbot.api.pipeline_report import build

    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        pid = p.id
    _seed(pid)
    with session_scope() as s:
        p = s.get(Project, pid)
        r = build(s, [(p, "owner")], 4, now=NOW)

    assert r["calendar"] == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"] and r["today"] == "2026-10-22"
    by = {x["name"]: x for x in r["pipelines"]}
    pos, ber = by["pl_positions"], by["pl_berths"]
    assert [d["ok"] for d in pos["daily"]] == [24, 24, 24, 10]  # 00:00…09:00 today
    assert pos["cadence"]["interval_s"] == 3600 and pos["cadence"]["overdue"] and pos["state"] == "late"
    assert pos["today"]["typical_by_now"] == 15 and pos["today"]["pace"] == round(10 / 15, 3)  # 00:00…14:00 on earlier days
    assert [d["ok"] + d["failed"] for d in ber["daily"]] == [4, 4, 4, 3] and ber["state"] == "unstable" and ber["success_rate"] == round(8 / 15, 4)  # 8 of the 15 runs since the 19th

    vol = r["volumes"][0]
    assert vol["table"] == "DB.RAW.POSITIONS" and vol["today"] == 10000 and vol["typical_by_now"] == 15000 and vol["avg_7d"] == 24000
    assert vol["daily"][0]["rows"] == 24000

    sig = r["errors"]["signatures"][0]
    assert sig["known"] and sig["title"].startswith("A file has more columns") and sig["count"] == sum(d["failed"] for d in ber["daily"])
    assert sig["pipelines"] == [{"project": "adfp", "name": "pl_berths"}] and "row number 7" in sig["sample"]
    assert sum(d["total"] for d in r["errors"]["daily"]) == sig["count"]
    assert sum(c["runs"] for row in r["heatmap"] for c in row) == sum(x["runs"] for x in r["pipelines"])

    # the run map: hourly slots over the 4 days, one cell per hour something ran in, now at 14:30 on the last day
    assert r["grid"] == {"slot_h": 1, "slots": 96, "now": 86.5}
    cells = {c["slot"]: c for c in pos["grid"]}
    assert len(cells) == 24 * 3 + 10 and cells[0]["ok"] == 1 and cells[0]["avg_s"] == 180 and cells[0]["rows"] == 1000
    assert sum(c["ok"] for c in pos["grid"]) == pos["ok"] and 86 not in cells  # stopped after 09:00 today
    bad = [c for c in ber["grid"] if c["failed"]]
    assert len(bad) == sum(d["failed"] for d in ber["daily"]) and bad[0]["error"].startswith("A file has more columns") and bad[0]["run_id"]
    assert all(c["avg_s"] is None for c in bad)  # only successful runs count towards the runtime

    kinds = [(i["kind"], i["title"]) for i in r["insights"]]
    assert kinds[0] == ("late", "pl_positions is overdue")  # the most urgent first
    assert ("unreliable", "pl_berths succeeds 53% of the time") in kinds
    assert any(k == "recurring" for k, _ in kinds)
    assert not any(k == "all_good" for k, _ in kinds)
    late = next(i for i in r["insights"] if i["kind"] == "late")
    assert "every 60 min" in late["detail"] and late["link"] == {"kind": "pipeline", "project": "adfp", "name": "pl_positions"}
    from alertbot.api.pipeline_report import _why
    assert _why(ERR, "pl_berths") == "A file has more columns than the dataset expects, in step Copy CSV."
    assert _why("Operation on target Load X failed: something odd happened", "p") == "Step Load X: something odd happened"
    assert all("raw_error" not in p["last"] for p in r["pipelines"])
    assert r["summary"]["states"]["unstable"] == 1 and r["summary"]["states"]["late"] == 1


def test_report_only_shows_my_projects(adf_ws):  # noqa: F811
    from alertbot.api.main import app

    with session_scope() as s:
        pid = s.query(Project).filter_by(key="adfp").one().id
        members = s.scalars(select(UserProject).where(UserProject.project_id == pid)).all()
        assert members
    _seed(pid)
    me = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com", "X-AlertBot": "1"})
    got = me.get("/api/pipeline-report?days=7").json()
    assert {p["name"] for p in got["pipelines"]} == {"pl_positions", "pl_berths"} and len(got["calendar"]) == 7
    other = TestClient(app, headers={"Authorization": "Bearer dev:other@contoso.com", "X-AlertBot": "1"})
    assert other.get("/api/pipeline-report").json()["pipelines"] == []
    assert me.get("/api/pipeline-report?days=1").status_code == 422
