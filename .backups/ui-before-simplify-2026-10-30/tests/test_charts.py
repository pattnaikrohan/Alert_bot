"""Charts people ask for in words: the AI proposes a spec from the catalogue, the bot computes it from its own records,
and pinned charts belong to one person only."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_adf import NOW, adf_ws  # noqa: F401 - adf_ws is a fixture

from alertbot.db import Incident, Project, Run, session_scope
from alertbot.engine.charts import ChartError, ChartSpec, compute


@pytest.fixture
def runs(adf_ws):  # noqa: F811
    with session_scope() as s:
        p = s.query(Project).filter_by(key="adfp").one()
        for d in range(20):
            for k, (pipe, status, mins) in enumerate((("pl_sql", "Succeeded", 5), ("pl_sql", "Failed" if d % 4 == 0 else "Succeeded", 7),
                                                      ("pl_copy", "Succeeded", 30))):
                at = NOW - timedelta(days=d, hours=k)
                s.add(Run(project_id=p.id, trigger_id=f"t{d}{k}", site=pipe, pipeline=pipe, trigger_status=status.lower(), run_id=f"r{d}-{k}",
                          run_status=status, requested_at=at, started_at=at, ended_at=at + timedelta(minutes=mins), duration_s=mins * 60))
        s.add(Incident(project_id=p.id, fingerprint="f1", scope="site:pl_sql", title="pl_sql failed", status="resolved", severity="critical",
                       opened_at=NOW - timedelta(days=3), resolved_at=NOW - timedelta(days=3) + timedelta(hours=4)))
        s.add(Incident(project_id=p.id, fingerprint="f2", scope="site:pl_copy", title="slow", status="open", severity="warning",
                       opened_at=NOW - timedelta(days=1)))
    return adf_ws


def _compute(spec: dict):
    with session_scope() as s:
        return compute(s, s.query(Project).filter_by(key="adfp").one(), ChartSpec.model_validate(spec), NOW + timedelta(minutes=1))


def test_the_catalogue_computes_from_the_bots_own_records(runs):
    failed = _compute({"title": "Failed runs", "viz": "stat", "metric": "failed_runs", "period_days": 30})
    assert failed["value"] == 5 and failed["unit"] == "runs"  # days 0, 4, 8, 12, 16
    by_day = _compute({"title": "Failed per day", "viz": "bar", "metric": "failed_runs", "period_days": 7, "group_by": "day"})
    assert len(by_day["series"]) == 8 and sum(x["value"] for x in by_day["series"]) == 2
    rate = _compute({"title": "Success", "viz": "bar", "metric": "success_rate", "period_days": 20, "group_by": "pipeline"})
    assert {x["label"]: x["value"] for x in rate["series"]} == {"pl_copy": 100.0, "pl_sql": 87.5}
    dur = _compute({"title": "Duration", "viz": "stat", "metric": "avg_duration_min", "period_days": 30, "filters": {"pipeline": "PL_COPY"}})
    assert dur["value"] == 30 and dur["resolved"]["pipeline"] == "pl_copy"  # names are matched without case
    week = _compute({"title": "Runs", "viz": "stat", "metric": "runs", "period_days": 7})
    assert week["value"] == 21 and week["previous"] == 21  # 7 x 24 h against the 7 x 24 h before
    assert _compute({"title": "Open", "viz": "stat", "metric": "open_incidents"})["value"] == 1
    assert _compute({"title": "MTTR", "viz": "stat", "metric": "time_to_resolve_h"})["value"] == 4
    with pytest.raises(ChartError, match="no pipeline called nope"):
        _compute({"title": "x", "viz": "stat", "metric": "runs", "filters": {"pipeline": "nope"}})


def test_specs_are_checked_against_the_catalogue():
    for bad in ({"title": "x", "viz": "stat", "metric": "revenue"},
                {"title": "x", "viz": "bar", "metric": "open_incidents", "group_by": "day"},
                {"title": "x", "viz": "stat", "metric": "empty_pct", "filters": {"table": "T"}},
                {"title": "x", "viz": "stat", "metric": "runs", "filters": {"sql": "DROP TABLE runs"}},
                {"title": "x", "viz": "radar", "metric": "runs"}):
        with pytest.raises(ValueError):
            ChartSpec.model_validate(bad)
    line = ChartSpec.model_validate({"title": "x", "viz": "line", "metric": "runs", "group_by": "pipeline"})
    assert line.viz == "bar"  # a line needs time on its axis
    assert ChartSpec.model_validate({"title": "x", "viz": "bar", "metric": "failed_runs"}).group_by == "day"


class SpyAI:
    def __init__(self, answers):
        self.answers, self.seen = list(answers), []

    def chat(self, messages, **_):
        self.seen.append(messages)
        return SimpleNamespace(text=json.dumps(self.answers.pop(0)), model="spy-model")


def test_asking_pinning_and_removing_are_per_person(runs, monkeypatch):
    from alertbot.ai import charts as ai_charts
    from alertbot.api.main import app

    me = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com"})
    spy = SpyAI([
        {"reply": "Failed runs or failed checks?", "spec": None},
        {"reply": "Here you go", "spec": {"title": "Failed runs, last 30 days", "viz": "bar", "metric": "failed_runs", "period_days": 30,
                                          "group_by": "fortnight"}},  # invalid: the bot asks the model to fix it once
        {"reply": "Here you go", "spec": {"title": "Failed runs, last 30 days", "viz": "bar", "metric": "failed_runs", "period_days": 30,
                                          "group_by": "week"}},
    ])
    monkeypatch.setattr(ai_charts, "client", lambda: spy)
    q = me.post("/api/projects/adfp/charts/ask", json={"messages": [{"role": "user", "content": "show me something useful"}]}).json()
    assert q["spec"] is None and "?" in q["reply"]  # nothing to go on: the model's question stands
    r = me.post("/api/projects/adfp/charts/ask", json={"messages": [
        {"role": "user", "content": "show failures"}, {"role": "assistant", "content": q["reply"]},
        {"role": "user", "content": "failed runs last month as a bar graph by week"}]}).json()
    assert r["spec"]["group_by"] == "week" and r["data"]["value"] == 5 and r["data"]["series"]
    sent = json.dumps(spy.seen)
    assert "pl_sql" in sent and "failed_runs" in sent and "r0-1" not in sent  # names and the catalogue, never records

    pinned = me.post("/api/projects/adfp/charts", json={"spec": r["spec"]}).json()["chart"]
    assert [c["id"] for c in me.get("/api/projects/adfp/charts").json()["charts"]] == [pinned["id"]]

    other = TestClient(app, headers={"Authorization": "Bearer dev:other@contoso.com"})
    assert other.get("/api/projects/adfp/charts").status_code == 404  # not even a member of this project
    with session_scope() as s:  # make "other" a member: they still see none of my charts and can't remove them
        from alertbot.db import User, UserProject

        u = s.query(User).filter_by(email="other@contoso.com").one()
        s.add(UserProject(user_id=u.id, project_id=s.query(Project).filter_by(key="adfp").one().id, role="viewer"))
    assert other.get("/api/projects/adfp/charts").json()["charts"] == []
    assert other.delete(f"/api/projects/adfp/charts/{pinned['id']}").status_code == 404
    assert me.delete(f"/api/projects/adfp/charts/{pinned['id']}").status_code == 200
    assert me.get("/api/projects/adfp/charts").json()["charts"] == []

    bad = me.post("/api/projects/adfp/charts", json={"spec": {"title": "x", "viz": "stat", "metric": "revenue"}})
    assert bad.status_code == 422


def test_without_the_ai_everyday_words_still_work(runs, monkeypatch):
    from alertbot.ai import charts as ai_charts
    from alertbot.ai.llm import AIUnavailable
    from alertbot.api.main import app

    def down():
        raise AIUnavailable("no model answered")

    monkeypatch.setattr(ai_charts, "client", down)
    me = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com"})
    r = me.post("/api/projects/adfp/charts/ask", json={"messages": [{"role": "user", "content": "runs per day this week as a bar chart"}]}).json()
    assert r["spec"]["metric"] == "runs" and r["spec"]["viz"] == "bar" and r["spec"]["period_days"] == 7 and r["model"] == "rules"
    no = me.post("/api/projects/adfp/charts/ask", json={"messages": [{"role": "user", "content": "revenue by country"}]}).json()
    assert no["spec"] is None  # business data is never charted



@pytest.mark.parametrize("text, metric, viz, group, days", [
    ("show me all the run duration a bar chart for each pipeline", "avg_duration_min", "bar", "pipeline", 30),
    ("run duration as a bar chart", "avg_duration_min", "bar", "day", 30),
    ("total runs in this month as a pie chart", "runs", "pie", "pipeline", 30),
    ("how many runs failed last week", "failed_runs", "stat", "none", 7),
    ("success rate gauge", "success_rate", "gauge", "none", 30),
    ("rows loaded per table over the last 14 days as a donut", "rows_loaded", "donut", "table", 14),
    ("incidents opened weekly trend this quarter", "incidents_opened", "line", "week", 90),
    ("longest run per pipeline horizontal bars", "max_duration_min", "hbar", "pipeline", 30),
    ("failed runs per day as an area chart", "failed_runs", "area", "day", 30),
])
def test_everyday_english_is_understood_without_a_model(text, metric, viz, group, days):
    from alertbot.ai.charts import guess

    s = guess(text, {"pipelines": ["pl_sql", "pl_copy"], "tables": ["DB.RAW.ORDERS"]})
    assert (s.metric, s.viz, s.group_by, s.period_days) == (metric, viz, group, days)


def test_the_models_near_misses_are_forgiven():
    from alertbot.ai.charts import normalise

    s = ChartSpec.model_validate(normalise({"title": "x", "viz": "Pie Chart", "metric": "run duration", "group_by": "pipelines",
                                            "days": "14", "filters": {"status": "FAILED runs", "extra": 1}}))
    assert (s.viz, s.metric, s.group_by, s.period_days, s.filters.status) == ("pie", "avg_duration_min", "pipeline", 14, "failed")
