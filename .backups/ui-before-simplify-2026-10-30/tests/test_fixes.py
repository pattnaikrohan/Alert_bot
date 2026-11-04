"""Fixing failures step by step, and learning: the library's steps carry this failure's details; "no, because…" picks
the approach that reason points to; "yes" makes that approach the first choice next time (and keeps what the person
did); errors the library doesn't know get AI-written steps, rethought with the reason when they don't work; and every
run is compared with its pipeline's normal duration."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from test_adf import NOW, adf_ws  # noqa: F401 - adf_ws is a fixture

from alertbot.db import Incident, MetricObservation, Project, Run, session_scope

MORE_COLUMNS = ("Operation on target CopyTerminal failed: ErrorCode=DelimitedTextMoreColumnsThanDefined,'Type=Microsoft.DataTransfer.Common.Shared."
                "HybridDeliveryException,Message=Error found when processing 'Csv/Tsv Format Text' source 'terminal.csv' with row number 22132: "
                "found more columns than expected column count 6.'")


def _api():
    from alertbot.api.main import app

    return TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com", "X-AlertBot": "1"})


def _failure(error=MORE_COLUMNS, run_id="r-1", site="DEV-SG-TERMINAL-CSV"):
    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        s.add(Run(project_id=p.id, trigger_id=run_id, site=site, pipeline="t", trigger_status="failed", run_id=run_id,
                  run_status="Failed", requested_at=NOW - timedelta(hours=1), error=error))
        inc = Incident(project_id=p.id, fingerprint=f"adfp:{run_id}", scope=f"site:{site}", title=f"{site} pipeline failed", severity="critical",
                       status="open", run_ref=run_id, checks=["run_failed"], opened_at=NOW - timedelta(hours=1))
        s.add(inc)
        s.flush()
        return inc.id


def test_known_errors_get_their_documented_fix_with_this_failures_details(adf_ws):  # noqa: F811
    iid = _failure()
    f = _api().post("/api/projects/adfp/fix", json={"incident_id": iid}).json()
    assert f["known"] and f["code"] == "DelimitedTextMoreColumnsThanDefined" and "Row 22132" in f["meaning"] and "the 6 the dataset" in f["meaning"]
    a = f["current"]
    assert a["source"] == "library" and a["variant"] == "data" and any("row 22132" in s["do"] for s in a["steps"])
    assert any("CopyTerminal" in s["do"] for s in a["steps"]) and f["docs"][0]["url"].startswith("https://learn.microsoft.com/")
    assert _api().post("/api/projects/adfp/fix", json={"incident_id": iid}).json()["current"]["id"] == a["id"]  # the same attempt, not a new one


def test_no_with_a_reason_picks_the_approach_it_points_to_and_yes_teaches_the_next_one(adf_ws):  # noqa: F811
    api = _api()
    iid = _failure()
    first = api.post("/api/projects/adfp/fix", json={"incident_id": iid}).json()["current"]
    nope = api.post(f"/api/projects/adfp/fix/{first['id']}/feedback", json={"worked": False, "reason": "the data is fine, I think the quote setting is wrong"}).json()
    second = nope["current"]
    assert second["variant"] == "format" and nope["history"][0]["status"] == "failed" and "quote" in nope["history"][0]["reason"]
    yes = api.post(f"/api/projects/adfp/fix/{second['id']}/feedback", json={"worked": True, "note": "Quote character was empty; set it to \""}).json()
    assert yes["celebrate"] and yes["current"]["status"] == "worked" and yes["team_notes"][0]["note"].startswith("Quote character")
    assert api.post(f"/api/projects/adfp/fix/{second['id']}/feedback", json={"worked": True}).status_code == 409
    # the next time this error happens, what worked here is tried first
    again = api.post("/api/projects/adfp/fix", json={"incident_id": _failure(run_id="r-2")}).json()
    assert again["current"]["variant"] == "format" and again["current_stats"] == {"worked": 1, "failed": 0}


def test_unknown_errors_get_ai_steps_rethought_with_the_reason_then_escalation(adf_ws, monkeypatch):  # noqa: F811
    from alertbot.ai import fixes

    asked = []

    def ai(error, ctx, tried):
        asked.append([t["reason"] for t in tried])
        if len(asked) > 2:
            return None
        return {"title": f"Approach {len(asked)}", "meaning": "", "steps": [{"do": "Check A"}, {"do": "Then B"}], "verify": "It runs.", "model": "m"}

    monkeypatch.setattr(fixes, "ai_steps", ai)
    api = _api()
    iid = _failure(error="ErrorCode=SomethingNobodyHasSeen,'Message=boom'", run_id="r-x")
    f = api.post("/api/projects/adfp/fix", json={"incident_id": iid}).json()
    assert not f["known"] and f["current"]["source"] == "ai" and f["current"]["title"] == "Approach 1"
    f = api.post(f"/api/projects/adfp/fix/{f['current']['id']}/feedback", json={"worked": False, "reason": "B isn't an option for us"}).json()
    assert f["current"]["title"] == "Approach 2" and asked[-1] == ["B isn't an option for us"]  # the AI rethinks with why
    f = api.post(f"/api/projects/adfp/fix/{f['current']['id']}/feedback", json={"worked": False, "reason": "still failing"}).json()
    assert f["current"]["source"] == "escalate" and any("Microsoft Q&A" in s["do"] for s in f["current"]["steps"])
    # an AI fix that worked becomes the team's fix for that error, offered first next time
    f = api.post(f"/api/projects/adfp/fix/{f['current']['id']}/feedback", json={"worked": False}).json()
    assert f["current"]["status"] == "failed"  # nothing left to try: it says so instead of looping


def test_the_assistant_answers_how_do_i_fix_it(adf_ws):  # noqa: F811
    iid = _failure()
    r = _api().post("/api/bot/chat", json={"message": f"how do I fix #{iid}?"}).json()
    assert "more columns" in r["text"] and "row 22132" in r["text"] and r["links"][0]["to"].endswith(f"/i/{iid}#fix")


def test_runs_are_compared_with_their_normal_duration(adf_ws, monkeypatch):  # noqa: F811
    from conftest import freeze_now

    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        for d in range(1, 15):
            at = NOW - timedelta(days=d)
            s.add(MetricObservation(project_id=p.id, output_id="_pipeline", site="DEV-MULTIFILE", metric="duration_s", field="",
                                    run_ref=f"h{d}", weekday=at.weekday(), value=300.0 + (d % 3) * 10, observed_at=at))
        s.add(Run(project_id=p.id, trigger_id="slow", site="DEV-MULTIFILE", pipeline="t", trigger_status="succeeded", run_id="slow",
                  run_status="Succeeded", requested_at=NOW - timedelta(minutes=30), started_at=NOW - timedelta(minutes=30), duration_s=900))
        s.add(Run(project_id=p.id, trigger_id="now", site="DEV-MULTIFILE", pipeline="t", trigger_status="running", run_id="now",
                  run_status="InProgress", requested_at=NOW - timedelta(minutes=2), started_at=NOW - timedelta(minutes=2)))
    freeze_now(monkeypatch, NOW)
    runs = {r["run_id"]: r for r in _api().get("/api/projects/adfp/runs").json()["runs"]}
    slow = runs["slow"]["vs_normal"]
    assert 300 <= slow["median_s"] <= 320 and slow["verdict"] == "much_slower" and slow["pct"] > 150
    live = runs["now"]["vs_normal"]
    assert live["running"] and live["verdict"] == "on_track" and 0.3 < live["progress"] < 0.5 and live["remaining_s"] > 0
