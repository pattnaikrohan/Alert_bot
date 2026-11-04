"""Data Factory projects, driven by a fake ADF REST API (same endpoints and JSON shapes as Azure, incl. paging)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from alertbot.db import Incident, MetricObservation, Run, session_scope
from alertbot.engine.cycle import backfill, run_cycle

NOW = datetime(2026, 10, 12, 16, 30)
FACTORY = "/subscriptions/sub-1/resourceGroups/rg-1/providers/Microsoft.DataFactory/factories/dev-factory"

PROJECT = """
project: adfp
display_name: CSDW Data Factory
kind: adf
pipeline:
  type: adf
  subscription_id: sub-1
  resource_group: rg-1
  factory: dev-factory
  auth: default_credential
alerts:
  notifier: power_automate
  flow_url: env://PA_ADF_URL
"""
MEMBERS = """
users:
  - email: chinmoy@contoso.com
    name: Chinmoy
    projects: { adfp: owner }
  - email: other@contoso.com
    name: Other
    projects: {}
"""

DEFINITIONS = {
    "DEV-SG-TERMINAL-CSV": [{"name": "CopyTerminal", "type": "Copy", "outputs": [{"referenceName": "TERMINAL_PROD"}]},
                            {"name": "CopyTerminalDev", "type": "Copy", "outputs": [{"referenceName": "TERMINAL_DEV"}],
                             "dependsOn": [{"activity": "CopyTerminal", "dependencyConditions": ["Succeeded"]}]}],
    "DEV-MULTIFILE": [
        {"name": "GetFiles", "type": "GetMetadata"},
        {"name": "LoopFiles", "type": "ForEach", "dependsOn": [{"activity": "GetFiles", "dependencyConditions": ["Succeeded"]}],
         "typeProperties": {"activities": [{"name": "CopyFile", "type": "Copy"}]}},
        {"name": "RunChild", "type": "ExecutePipeline", "typeProperties": {"pipeline": {"referenceName": "DEV-CHILD"}}},
    ],
    "DEV-CHILD": [{"name": "Merge", "type": "SqlServerStoredProcedure"}],
    "DEV-EMPTY": [
        {"name": "GM_BA", "type": "GetMetadata"},
        {"name": "Filter_BA", "type": "Filter", "dependsOn": [{"activity": "GM_BA", "dependencyConditions": ["Succeeded"]}]},
        {"name": "BA PROD", "type": "Copy", "outputs": [{"referenceName": "BA_ID_PROD"}],
         "dependsOn": [{"activity": "Filter_BA", "dependencyConditions": ["Succeeded"]}]},
        {"name": "BA DEV", "type": "Copy", "outputs": [{"referenceName": "BA_ID_DEV"}]},
        {"name": "CNTR PROD", "type": "Copy", "outputs": [{"referenceName": "CNTR_PROD"}]},
    ],
}
TRIGGERS = [
    {"name": "TR_DAILY_1500", "properties": {"type": "ScheduleTrigger", "runtimeState": "Started",
     "pipelines": [{"pipelineReference": {"referenceName": "DEV-SG-TERMINAL-CSV"}}],
     "typeProperties": {"recurrence": {"frequency": "Day", "interval": 1, "schedule": {"hours": [15], "minutes": [0]}}}}},
    {"name": "TR_OLD", "properties": {"type": "ScheduleTrigger", "runtimeState": "Stopped",
     "pipelines": [{"pipelineReference": {"referenceName": "DEV-MULTIFILE"}}],
     "typeProperties": {"recurrence": {"frequency": "Hour", "interval": 1}}}},
]


def z(dt: datetime) -> str:
    return dt.isoformat() + ".1234567Z"  # ADF sends 7 fractional digits


class FakeAdf:
    def __init__(self):
        self.runs: list[dict] = []
        self.acts: dict[str, list[dict]] = {}
        self.n = 0

    def add(self, pipeline: str, start: datetime, minutes: float = 5, status: str = "Succeeded", rows: int | None = None,
            fail_iteration: int | None = None, message: str = "", columns_error: bool = False, skipped: int = 0) -> str:
        self.n += 1
        rid = f"00000000-0000-0000-0000-{self.n:012d}"
        end = start + timedelta(minutes=minutes)
        self.runs.append({"runId": rid, "pipelineName": pipeline, "status": status, "runStart": z(start),
                          "runEnd": z(end) if status != "InProgress" else None, "durationInMs": int(minutes * 60000),
                          "lastUpdated": z(end), "message": message,
                          "invokedBy": {"name": "CSV_TRIGGER", "invokedByType": "BlobEventsTrigger"} if pipeline == "DEV-EMPTY"
                else {"name": "TR_DAILY_1500", "invokedByType": "ScheduleTrigger"}})
        acts = []
        if pipeline == "DEV-SG-TERMINAL-CSV":
            acts.append(self._act("CopyTerminal", "Copy", start, end, status, {"rowsRead": rows, "rowsCopied": rows, "rowsSkipped": 0}))
            acts.append(self._act("CopyTerminalDev", "Copy", start, end, status,
                                  {"rowsRead": rows, "rowsCopied": (rows or 0) - skipped, "rowsSkipped": skipped}))
        elif pipeline == "DEV-EMPTY":
            failed_empty = status == "Failed" and not columns_error
            acts.append(self._act("GM_BA", "GetMetadata", start, start, "Succeeded",
                                  {"itemName": "BA_ID", "childItems": [{"name": "test_file.txt" if failed_empty else "BA_ID_1.CSV"}]}))
            acts.append(self._act("Filter_BA", "Filter", start, start, "Succeeded",
                                  {"ItemsCount": 1, "FilteredItemsCount": 0 if failed_empty else 1, "Value": []},
                                  inp={"items": "@activity('GM_BA').output.childItems", "condition": "@endswith(item().name,'.CSV')"}))
            if failed_empty:
                acts.append(self._act("BA PROD", "Copy", start, end, "Failed", {}, {
                    "errorCode": "InvalidTemplate", "message": "The expression 'replace(activity('Filter_BA').output.value[0].name, 'a','b')' "
                    "cannot be evaluated because array index '0' cannot be selected from empty array."},
                    inp={"source": {"storeSettings": {"wildcardFolderPath": "inbound/BA_ID", "wildcardFileName": "*.CSV"}},
                         "sink": {"type": "SnowflakeV2Sink"}}))
            else:
                acts.append(self._act("BA PROD", "Copy", start, end, "Succeeded", {"rowsRead": 10, "rowsCopied": 10}))
            acts.append(self._act("CNTR PROD", "Copy", start, end, "Failed" if columns_error else "Succeeded",
                                  {} if columns_error else {"rowsRead": 500, "rowsCopied": 500, "filesRead": 2},
                                  {"errorCode": "2200", "message": "ErrorCode=DelimitedTextMoreColumnsThanDefined,'Error found when processing'"}
                                  if columns_error else None))
        elif pipeline == "DEV-MULTIFILE":
            acts.append(self._act("GetFiles", "GetMetadata", start, start, "Succeeded", {}))
            for i in range(1, 4):
                failed = fail_iteration == i
                acts.append(self._act("CopyFile", "Copy", start, end, "Failed" if failed else "Succeeded",
                                      {"rowsCopied": 0 if failed else 100},
                                      {"errorCode": "2200", "message": "ErrorCode=UserErrorFileNotFound, path /in/file3.csv does not exist."}
                                      if failed else None))
            if fail_iteration:
                acts.append(self._act("LoopFiles", "ForEach", start, end, "Failed", {},
                                      {"errorCode": "ActivityFailed", "message": "Activity failed because an inner activity failed"}))
            else:
                acts.append(self._act("RunChild", "ExecutePipeline", start, end, "Succeeded", {"pipelineRunId": "child-" + rid}))
                self.acts["child-" + rid] = [self._act("Merge", "SqlServerStoredProcedure", start, end, "Succeeded", {})]
        self.acts[rid] = acts
        return rid

    @staticmethod
    def _act(name, typ, start, end, status, output, error=None, inp=None) -> dict:  # noqa: ANN001
        return {"activityName": name, "activityType": typ, "status": status, "activityRunStart": z(start),
                "activityRunEnd": z(end), "durationInMs": int((end - start).total_seconds() * 1000),
                "input": inp or {}, "output": output, "error": error or {"errorCode": "", "message": ""}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        path = request.url.path
        assert path.startswith(FACTORY), path
        rest = path[len(FACTORY):]
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and rest == "/pipelines":
            return httpx.Response(200, json={"value": [{"name": n} for n in ("DEV-SG-TERMINAL-CSV", "DEV-MULTIFILE", "DEV-EMPTY")]})
        if request.method == "GET" and (m := re.fullmatch(r"/pipelines/(.+)", rest)):
            return httpx.Response(200, json={"name": m.group(1), "properties": {"activities": DEFINITIONS[m.group(1)]}})
        if request.method == "GET" and (m := re.fullmatch(r"/datasets/(.+)", rest)):
            name = m.group(1)
            env = "DEV" if name.endswith("_DEV") else "PROD"
            return httpx.Response(200, json={"name": name, "properties": {
                "type": "SnowflakeV2Table", "linkedServiceName": {"referenceName": f"SF_{env}"},
                "typeProperties": {"schema": "RAW", "table": name.rsplit("_", 1)[0]}}})
        if request.method == "GET" and rest == "/linkedservices":
            return httpx.Response(200, json={"value": [
                {"name": f"SF_{e}", "properties": {"type": "SnowflakeV2", "typeProperties": {"database": f"DB_{e}", "password": "never-read"}}}
                for e in ("PROD", "DEV")]})
        if request.method == "GET" and rest == "/triggers":
            return httpx.Response(200, json={"value": TRIGGERS})
        if request.method == "POST" and rest == "/queryPipelineRuns":
            lo, hi = body["lastUpdatedAfter"].rstrip("Z"), body["lastUpdatedBefore"].rstrip("Z")
            names = set(body.get("filters", [{}])[0].get("values", [])) if body.get("filters") else None
            hits = [r for r in self.runs if lo <= r["lastUpdated"].rstrip("Z")[:26] < hi and (names is None or r["pipelineName"] in names)]
            page = int(body.get("continuationToken") or 0)
            chunk = hits[page * 25:(page + 1) * 25]
            more = (page + 1) * 25 < len(hits)
            return httpx.Response(200, json={"value": chunk, **({"continuationToken": str(page + 1)} if more else {})})
        if request.method == "POST" and (m := re.fullmatch(r"/pipelineruns/(.+)/queryActivityruns", rest)):
            return httpx.Response(200, json={"value": self.acts.get(m.group(1), [])})
        if request.method == "GET" and (m := re.fullmatch(r"/pipelineruns/(.+)", rest)):
            hit = next((r for r in self.runs if r["runId"] == m.group(1)), None)
            return httpx.Response(200, json=hit) if hit else httpx.Response(404)
        return httpx.Response(404, json={"error": {"message": f"unexpected {request.method} {rest}"}})


class FakeCredential:
    def get_token(self, *_a, **_k):  # noqa: ANN002, ANN003
        return type("T", (), {"token": "fake-token"})()


@pytest.fixture
def adf_ws(tmp_path, monkeypatch):
    from conftest import _clear_caches

    from alertbot import connectors
    from alertbot.connectors.adf import AdfPipeline

    cfg = tmp_path / "config"
    (cfg / "projects").mkdir(parents=True)
    (cfg / "projects" / "adfp.yaml").write_text(PROJECT, encoding="utf-8")
    (cfg / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALERTBOT_DB_URL", f"sqlite:///{(tmp_path / 'alertbot.db').as_posix()}")
    monkeypatch.setenv("ALERTBOT_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("ALERTBOT_AUTH_MODE", "dev")
    monkeypatch.setenv("PA_ADF_URL", "")
    _clear_caches()
    fake = FakeAdf()
    monkeypatch.setattr(connectors, "AdfPipeline",
                        lambda c: AdfPipeline(c, credential=FakeCredential(), transport=httpx.MockTransport(fake.handler)))
    from alertbot.db import create_all
    from alertbot.seed import seed

    create_all()
    with session_scope() as s:
        seed(s)
    yield fake
    from alertbot.db.session import get_engine

    get_engine().dispose()
    _clear_caches()


def history(fake: FakeAdf, days: int = 20, last_rows: int | None = None, skip_last: int = 0, multifile_fail: bool = False,
            last_skipped: int = 0) -> None:
    for d in range(days, -1, -1):
        day = (NOW - timedelta(days=d)).replace(hour=15, minute=0, second=0, microsecond=0)
        if day > NOW or d < skip_last:
            continue
        rows = last_rows if d == 0 and last_rows is not None else 1000 + (d % 5) * 10
        fake.add("DEV-SG-TERMINAL-CSV", day, minutes=5 + (d % 3) * 0.5, rows=rows, skipped=last_skipped if d == 0 else 0)
        if d % 2 == 0:
            fake.add("DEV-MULTIFILE", day.replace(hour=9), minutes=3,
                     status="Failed" if multifile_fail and d == 0 else "Succeeded",
                     fail_iteration=3 if multifile_fail and d == 0 else None,
                     message="Operation on target LoopFiles failed" if multifile_fail and d == 0 else "")


def cycle():
    from alertbot.config import load_projects

    cfg = load_projects()["adfp"]
    backfill(cfg, days=21, now=NOW - timedelta(hours=8))
    r = run_cycle(cfg, now=NOW)
    assert r.status == "ok", r.error
    return r


def incidents():
    with session_scope() as s:
        return {i.title: i for i in s.scalars(select(Incident))}


def test_healthy_factory_learns_volumes_and_opens_nothing(adf_ws):
    history(adf_ws)
    r = cycle()
    assert r.runs > 0 and incidents() == {}
    with session_scope() as s:
        runs = list(s.scalars(select(Run)))
        assert {r.site for r in runs} == {"DEV-SG-TERMINAL-CSV", "DEV-MULTIFILE"}
        assert all(r.run_status == "Succeeded" and r.observed for r in runs)
        copied = s.scalars(select(MetricObservation.value).where(MetricObservation.metric == "row_count",
                                                               MetricObservation.output_id == "table:DB_PROD.RAW.TERMINAL")).all()
        assert len(copied) >= 15 and all(900 < v < 1100 for v in copied)


def test_failure_is_traced_to_the_exact_step(adf_ws):
    history(adf_ws, multifile_fail=True)
    cycle()
    inc = incidents()["DEV-MULTIFILE pipeline failed"]
    assert inc.severity == "critical"
    assert "'CopyFile'" in inc.likely_cause and "wasn't there" in inc.likely_cause
    assert "adf.azure.com/en/monitoring/pipelineruns/" in inc.suggested_action
    where = next(e for e in inc.evidence if e["label"] == "Where it failed")
    assert where["detail"] == "ForEach 'LoopFiles' → Copy 'CopyFile' (iteration 3 of 3)"


def test_low_copy_volume_and_missed_schedule(adf_ws):
    history(adf_ws, last_rows=40)
    cycle()
    assert "DEV-SG-TERMINAL-CSV copied unusually few rows" in incidents()


def test_missed_schedule_uses_the_factorys_trigger(adf_ws):
    history(adf_ws, skip_last=3)  # the daily 15:00 trigger stopped firing three days ago
    cycle()
    inc = incidents()["DEV-SG-TERMINAL-CSV missed its schedule"]
    assert "TR_DAILY_1500" in inc.likely_cause
    # DEV-MULTIFILE only has a *stopped* trigger, so it is treated as manual: no schedule alarm for it
    assert not any(t.startswith("DEV-MULTIFILE") for t in incidents())


def test_failure_tree_api_and_access(adf_ws):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    history(adf_ws, multifile_fail=True)
    cycle()
    with session_scope() as s:
        rid = s.scalars(select(Run.run_id).where(Run.site == "DEV-MULTIFILE", Run.run_status == "Failed")).first()
        ok = s.scalars(select(Run.run_id).where(Run.site == "DEV-MULTIFILE", Run.run_status == "Succeeded")).first()
    c = TestClient(app)
    h = {"Authorization": "Bearer dev:chinmoy@contoso.com"}
    tree = c.get(f"/api/projects/adfp/runs/{rid}/tree", headers=h).json()
    assert [p["name"] for p in tree["failure_path"]] == ["LoopFiles", "CopyFile"]
    assert tree["monitor_url"].endswith(f"factory={FACTORY}")
    good = c.get(f"/api/projects/adfp/runs/{ok}/tree", headers=h).json()
    child = next(s for s in good["steps"] if s["name"] == "RunChild")
    assert child["called"][0]["pipeline"] == "DEV-CHILD" and child["called"][0]["steps"][0]["status"] == "Succeeded"
    assert c.get(f"/api/projects/adfp/runs/{rid}/tree", headers={"Authorization": "Bearer dev:other@contoso.com"}).status_code == 404
    answer = c.post("/api/ask", json={"question": "why did DEV-MULTIFILE fail?"}, headers=h).json()["answer"]
    assert answer.startswith("DEV-MULTIFILE pipeline failed") and "CopyFile" in answer


def test_failed_run_gets_a_detailed_story(adf_ws):
    """The CSDW case: a storage event starts the run, one inbound folder only holds test_file.txt, its Filter keeps
    nothing and the Copy fails on value[0]; the same pipeline also had a different, recurring problem days before."""
    for d in range(8, 0, -1):
        day = (NOW - timedelta(days=d)).replace(hour=18, minute=10, second=0, microsecond=0)
        bad = d in (2, 3, 4)
        adf_ws.add("DEV-EMPTY", day, status="Failed" if bad else "Succeeded", columns_error=bad,
                   message="Operation on target CNTR PROD failed: ErrorCode=DelimitedTextMoreColumnsThanDefined,'x'" if bad else "")
    adf_ws.add("DEV-EMPTY", NOW - timedelta(hours=3), status="Failed",
               message="Operation on target BA PROD failed: The expression ... cannot be selected from empty array.")
    cycle()
    inc = incidents()["DEV-EMPTY pipeline failed"]
    assert inc.summary.startswith("This run started") and "BlobEventsTrigger" in inc.summary
    assert "'CNTR PROD' succeeded: copied 500 rows" in inc.summary
    assert "didn't run" in inc.summary and "'BA DEV'" in inc.summary
    cause = inc.likely_cause
    assert cause.startswith("Nothing new to load in 1 input folder (BA_ID)")
    assert "'BA PROD' ← 'Filter_BA' kept 0 of 1 item(s); the folder only held test_file.txt." in cause
    assert "endswith(item().name,'.CSV')" in cause and "storage event (CSV_TRIGGER)" in cause
    assert "Not refreshed by this run: BA_ID_PROD (Snowflake)." in inc.impact
    assert "4 of its last 9 runs failed" in inc.impact
    assert "3× a source file has more columns than the dataset defines" in inc.impact
    action = inc.suggested_action
    assert action.startswith("1. Check whether new files were expected in BA_ID.")
    assert "If Condition such as @greater(activity('Filter_BA').output.FilteredItemsCount, 0)" in action
    assert "test_file.txt" in action and "Separately, 'CNTR PROD' has failed 3×" in action
    assert "adf.azure.com" in action
    labels = [e["label"] for e in inc.evidence]
    assert labels[0] == "Run" and "What each Filter found" in labels and "Folder BA_ID" in labels

    # the run view: arrows between steps and what each step did, for the ADF-Studio-style graph
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    h = {"Authorization": "Bearer dev:chinmoy@contoso.com"}
    tree = TestClient(app).get(f"/api/projects/adfp/runs/{inc.run_ref}/tree", headers=h).json()
    steps = {s["name"]: s for s in tree["steps"]}
    assert steps["BA PROD"]["depends_on"] == [{"activity": "Filter_BA", "conditions": ["Succeeded"]}]
    assert steps["BA PROD"]["writes"] == ["BA_ID_PROD"] and steps["BA DEV"]["status"] == "NotRun"
    assert steps["Filter_BA"]["runs"][0]["facts"] == ["Kept 0 of 1 item(s)", "Condition endswith(item().name,'.CSV')"]
    assert steps["GM_BA"]["runs"][0]["facts"] == ["Folder BA_ID", "1 item(s): test_file.txt"]
    assert steps["BA PROD"]["runs"][0]["facts"] == ["Reads inbound/BA_ID/*.CSV", "Writes to Snowflake"]
    runs = TestClient(app).get("/api/projects/adfp/runs", headers=h).json()
    assert runs["kind"] == "adf"


def test_every_table_is_tracked_with_its_own_normal(adf_ws, monkeypatch):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    history(adf_ws, last_rows=40)
    cycle()
    monkeypatch.setattr("alertbot.api.views.utcnow", lambda: NOW + timedelta(hours=1))  # the views look back from the history's "now"
    titles = incidents()
    # one pipeline, two target tables in two databases, each judged on its own
    assert "DB_PROD.RAW.TERMINAL loaded unusually few rows" in titles
    assert "DB_DEV.RAW.TERMINAL loaded unusually few rows" in titles
    h = {"Authorization": "Bearer dev:chinmoy@contoso.com"}
    loads = TestClient(app).get("/api/projects/adfp/loads", headers=h).json()["tables"]
    by = {t["table"]: t for t in loads}
    prod = by["DB_PROD.RAW.TERMINAL"]
    assert prod["env"] == "PROD" and prod["steps"] == ["CopyTerminal"] and prod["dataset"] == "TERMINAL_PROD"
    assert prod["last"]["rows_copied"] == 40 and 1000 <= prod["normal"]["median"] <= 1050 and prod["loads_count"] == 21
    assert prod["status"] == "critical" and prod["incident"]["title"] == "DB_PROD.RAW.TERMINAL loaded unusually few rows"
    assert by["DB_DEV.RAW.TERMINAL"]["env"] == "DEV"
    # the run view lists what each table got from the run
    with session_scope() as s:
        rid = s.scalars(select(Run.run_id).where(Run.site == "DEV-SG-TERMINAL-CSV").order_by(Run.requested_at.desc())).first()
    tables = TestClient(app).get(f"/api/projects/adfp/runs/{rid}/tree", headers=h).json()["tables"]
    assert [(t["table"], t["rows_copied"]) for t in tables] == [("DB_PROD.RAW.TERMINAL", 40), ("DB_DEV.RAW.TERMINAL", 40)]


def test_skipped_rows_open_a_table_incident(adf_ws):
    history(adf_ws, last_skipped=12)
    cycle()
    inc = incidents()["DB_DEV.RAW.TERMINAL load skipped rows"]
    assert inc.severity == "warning" and "skipped rows it couldn't write" in inc.likely_cause
    assert "DB_PROD.RAW.TERMINAL load skipped rows" not in incidents()


def test_management_overview_answers_the_big_questions(adf_ws, monkeypatch):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    history(adf_ws, multifile_fail=True)
    cycle()
    monkeypatch.setattr("alertbot.api.views.utcnow", lambda: NOW + timedelta(hours=1))  # the views look back from the history's "now"
    h = {"Authorization": "Bearer dev:chinmoy@contoso.com"}
    m = TestClient(app).get("/api/projects/adfp/management?days=30", headers=h).json()
    k = m["kpis"]
    assert k["runs"] == 32 and k["failed"] == 1 and 96 < k["success_rate"] < 97
    # two named Snowflake tables, plus DEV-MULTIFILE's copy step, whose dataset isn't named, tracked under its step name
    assert k["tables"] == 3 and k["open_critical"] == 1
    assert m["status"]["level"] == "critical" and m["status"]["headline"].startswith("Pipelines succeeded 97%")
    assert m["failure_reasons"][0]["reason"] == "a step inside 'LoopFiles' failed"
    assert {p["name"] for p in m["pipelines"]} == {"DEV-SG-TERMINAL-CSV", "DEV-MULTIFILE", "DEV-EMPTY"}  # DEV-EMPTY: listed, never run
    assert len(m["daily"]) == 30 and sum(d["succeeded"] + d["failed"] for d in m["daily"]) == 32
    assert {t["table"] for t in m["tables"]} == {"DB_PROD.RAW.TERMINAL", "DB_DEV.RAW.TERMINAL", "COPYFILE"}
    assert m["incidents"][0]["title"] == "DEV-MULTIFILE pipeline failed"
    assert TestClient(app).get("/api/projects/adfp/management", headers={"Authorization": "Bearer dev:other@contoso.com"}).status_code == 404


def test_every_pipeline_is_listed_even_without_runs(adf_ws, monkeypatch):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    history(adf_ws)  # DEV-EMPTY never runs in this history
    cycle()
    monkeypatch.setattr("alertbot.api.views.utcnow", lambda: NOW + timedelta(hours=1))  # the views look back from the history's "now"
    h = {"Authorization": "Bearer dev:chinmoy@contoso.com"}
    c = TestClient(app)
    v = c.get("/api/pipelines", headers=h).json()
    by = {i["name"]: i for i in v["items"]}
    assert set(by) == {"DEV-SG-TERMINAL-CSV", "DEV-MULTIFILE", "DEV-EMPTY"}
    assert by["DEV-EMPTY"]["state"] == "never_run" and by["DEV-EMPTY"]["last_run"] is None
    assert by["DEV-SG-TERMINAL-CSV"]["state"] == "healthy" and by["DEV-SG-TERMINAL-CSV"]["service"] == "Data Factory"
    assert by["DEV-SG-TERMINAL-CSV"]["triggers"] == [{"name": "TR_DAILY_1500", "kind": "Schedule", "started": True, "every_hours": 24.0}]
    assert by["DEV-MULTIFILE"]["triggers"][0]["started"] is False  # a stopped trigger is still shown
    assert v["counts"]["never_run"] == 1 and v["counts"]["total"] == 3
    # someone who isn't on the project sees none of it
    assert TestClient(app).get("/api/pipelines", headers={"Authorization": "Bearer dev:other@contoso.com"}).json()["items"] == []

    m = c.get("/api/projects/adfp/management", headers=h).json()
    empty = next(p for p in m["pipelines"] if p["name"] == "DEV-EMPTY")
    assert empty["runs"] == 0 and empty["last_status"] is None
    assert not any("DEV-EMPTY" in r for r in m["status"]["reasons"])  # never run is not "failed"


def test_table_loads_show_exactly_how_many_rows_were_inserted(adf_ws, monkeypatch):
    from fastapi.testclient import TestClient

    from alertbot.api import views
    from alertbot.api.main import app

    monkeypatch.setattr(views, "utcnow", lambda: NOW)  # "today" is the test's day

    history(adf_ws)  # each day at 15:00: 1000 + (d % 5) * 10 rows into both TERMINAL tables
    cycle()
    h = {"Authorization": "Bearer dev:chinmoy@contoso.com"}
    v = TestClient(app).get("/api/projects/adfp/loads?days=7", headers=h).json()
    prod = next(t for t in v["tables"] if t["table"] == "DB_PROD.RAW.TERMINAL")
    assert prod["inserted"] == 7110 and prod["loads_count"] == 7  # today and the 6 days before
    assert prod["inserted_prev"] == 7150  # the 7 days before that
    assert len(prod["daily"]) == 7 and prod["daily"][-1] == {"date": "2026-10-12", "rows": 1000.0}
    assert prod["loads"][0]["at"].startswith("2026-10-12T15:0") and prod["loads"][0]["run_status"] == "Succeeded"
    assert v["from"] == "2026-10-06" and v["to"] == "2026-10-12"
    assert v["totals"]["inserted"] >= 2 * 7110 and v["totals"]["tables_loaded"] >= 2
    today = TestClient(app).get("/api/projects/adfp/loads?days=1", headers=h).json()
    assert next(t for t in today["tables"] if t["table"] == "DB_PROD.RAW.TERMINAL")["inserted"] == 1000
    assert len(today["daily"]) == 1 and today["daily"][0]["top"][0]["rows"] == 1000


def test_new_runs_are_noticed_without_a_refresh(adf_ws):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app
    from alertbot.config import load_projects
    from alertbot.engine.watch import look, watch_all

    history(adf_ws)
    cycle()
    cfg = load_projects()["adfp"]
    t0 = NOW + timedelta(minutes=1)
    assert watch_all(t0) == []  # nothing new in Azure: no extra round
    assert look(cfg, t0 + timedelta(seconds=10)) is None  # looked a moment ago: not yet

    # the pipeline fails in Azure; within one look the bot has checked it and opened an incident
    adf_ws.add("DEV-MULTIFILE", t0, minutes=3, status="Failed", fail_iteration=3, message="Operation on target LoopFiles failed")
    out = watch_all(t0 + timedelta(minutes=4))
    assert [(c.pipeline, c.status, c.before) for _, changes, _ in out for c in changes] == [("DEV-MULTIFILE", "Failed", None)]
    assert "DEV-MULTIFILE pipeline failed" in incidents()

    # it's fixed and rerun: the next look sees the success and the incident resolves, still without a refresh
    adf_ws.add("DEV-MULTIFILE", t0 + timedelta(minutes=10), minutes=3)
    out = watch_all(t0 + timedelta(minutes=14))
    assert [c.status for _, changes, _ in out for c in changes] == ["Succeeded"]
    assert incidents()["DEV-MULTIFILE pipeline failed"].status == "resolved"

    # the portal is told what happened
    feed = TestClient(app).get("/api/feed", headers={"Authorization": "Bearer dev:chinmoy@contoso.com"}).json()
    assert [e["status"] for e in feed["live"]["events"][:2]] == ["Succeeded", "Failed"]
    assert feed["live"]["events"][0]["pipeline"] == "DEV-MULTIFILE" and feed["live"]["last_look_at"]
