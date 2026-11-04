"""Everyone who signs in sees the Azure pipelines *they* can see: Data Factory, Synapse and Logic Apps, found with their
own sign-in (Azure Resource Graph), watched read-only, and removed again when Azure stops showing them."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from test_adf import NOW, FakeCredential, adf_ws  # noqa: F401 - adf_ws is a fixture

from alertbot.db import AzureAccess, Incident, Project, User, UserProject, session_scope

SUB = "sub-1"
FACTORY_ID = f"/subscriptions/{SUB}/resourceGroups/rg-1/providers/Microsoft.DataFactory/factories/dev-factory"
GRAPH_ROWS = [
    {"id": FACTORY_ID, "name": "dev-factory", "type": "microsoft.datafactory/factories", "resourceGroup": "rg-1", "subscriptionId": SUB},
    {"id": f"/subscriptions/{SUB}/resourceGroups/rg-syn/providers/Microsoft.Synapse/workspaces/analytics-ws", "name": "analytics-ws",
     "type": "microsoft.synapse/workspaces", "resourceGroup": "rg-syn", "subscriptionId": SUB},
    {"id": f"/subscriptions/{SUB}/resourceGroups/rg-la/providers/Microsoft.Logic/workflows/wf-orders", "name": "wf-orders",
     "type": "microsoft.logic/workflows", "resourceGroup": "rg-la", "subscriptionId": SUB},
    {"id": f"/subscriptions/{SUB}/resourceGroups/rg-la/providers/Microsoft.Logic/workflows/wf-off", "name": "wf-off",
     "type": "microsoft.logic/workflows", "resourceGroup": "rg-la", "subscriptionId": SUB},
]


def graph(rows, fail=False):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer user-token"
        if request.url.path == "/providers/Microsoft.ResourceGraph/resources":
            if fail:
                return httpx.Response(403, json={"error": {"message": "no graph"}})
            body = json.loads(request.content)
            assert "microsoft.logic/workflows" in body["query"]
            return httpx.Response(200, json={"data": rows, "count": len(rows)})
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"value": [{"subscriptionId": SUB, "state": "Enabled"}]})
        if request.url.path == f"/subscriptions/{SUB}/resources":
            return httpx.Response(200, json={"value": [{"id": r["id"], "name": r["name"], "type": r["type"]} for r in rows]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture
def people(adf_ws, monkeypatch):  # noqa: F811
    from alertbot import msauth

    monkeypatch.setattr(msauth, "user_token", lambda hid, scope=msauth.ARM_SCOPE: ("user-token", 0))
    with session_scope() as s:
        ids = {u.email: u.id for u in s.scalars(select(User))}
    return ids


def _discover(user_id, rows, fail=False):
    from alertbot.discovery import discover_user

    return discover_user(user_id, f"hid-{user_id}", transport=graph(rows, fail))


def _projects(user_id):
    with session_scope() as s:
        return {p.key: role for p, role in s.execute(select(Project, UserProject.role).join(UserProject).where(UserProject.user_id == user_id))}


def test_each_person_sees_what_azure_shows_them(people, tmp_path):
    other = people["other@contoso.com"]
    assert _projects(other) == {}  # the screenshot: signed in, no project

    r = _discover(other, GRAPH_ROWS)
    assert r["found"] == 3  # the factory, the workspace, and the resource group's Logic Apps (one project)
    mine = _projects(other)
    # the hand-written project for the same factory is joined, not duplicated
    assert mine == {"adfp": "viewer", "syn-analytics-ws": "owner", "la-rg-la": "owner"}
    files = sorted(f.name for f in (tmp_path / "config" / "projects" / "discovered").glob("*.yaml"))
    assert files == ["la-rg-la.yaml", "syn-analytics-ws.yaml"]

    from alertbot.config import load_projects

    syn = load_projects()["syn-analytics-ws"]
    assert syn.pipeline.type == "synapse" and syn.pipeline.workspace == "analytics-ws" and syn.pipeline.auth == "signed_in_users"
    assert load_projects()["la-rg-la"].display_name == "Logic Apps · rg-la"

    # someone else who can see the workspace joins it as a viewer; the creator stays owner
    chinmoy = people["chinmoy@contoso.com"]
    _discover(chinmoy, GRAPH_ROWS[1:2])
    assert _projects(chinmoy) == {"adfp": "owner", "syn-analytics-ws": "viewer"}

    # Azure stops showing the Logic Apps to `other`: their automatic membership goes; hand-written ownership stays
    r = _discover(other, GRAPH_ROWS[:2])
    assert r["removed"] == ["la-rg-la"] and "la-rg-la" not in _projects(other)
    with session_scope() as s:
        assert sorted(s.scalars(select(AzureAccess.project_key).where(AzureAccess.user_id == other))) == ["adfp", "syn-analytics-ws"]


def test_without_resource_graph_each_subscription_is_listed(people):
    r = _discover(people["other@contoso.com"], GRAPH_ROWS, fail=True)
    assert r["found"] == 3


def test_discovery_status_and_api(people):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    other = people["other@contoso.com"]
    _discover(other, GRAPH_ROWS)
    c = TestClient(app, headers={"Authorization": "Bearer dev:other@contoso.com"})
    me = c.get("/api/me").json()
    assert me["discovery"]["status"] == "done"
    assert me["discovery"]["message"] == "Found 1 Data Factory, 1 Synapse workspace, 1 resource group with Logic Apps."
    services = {p["key"]: p["service"] for p in me["projects"]}
    assert services == {"adfp": "Data Factory", "syn-analytics-ws": "Synapse", "la-rg-la": "Logic Apps"}
    # the person's own sign-in is used for the monitoring of what they found, most recent first
    from alertbot.connectors.azure_auth import _accounts_for

    assert _accounts_for(GRAPH_ROWS[1]["id"])[0] == f"hid-{other}"


def test_a_failed_sign_in_is_reported_not_raised(people, monkeypatch):
    from alertbot import msauth
    from alertbot.discovery import discover_user, status

    def expired(hid, scope=msauth.ARM_SCOPE):
        raise msauth.SignInRequired("this person's Microsoft sign-in has expired")

    monkeypatch.setattr(msauth, "user_token", expired)
    other = people["other@contoso.com"]
    assert "expired" in discover_user(other, "hid")["error"]
    with session_scope() as s:
        assert status(s, other)["status"] == "error"


# ── Logic Apps ───────────────────────────────────────────────────────────────

LA_BASE = f"/subscriptions/{SUB}/resourceGroups/rg-la/providers/Microsoft.Logic/workflows"
DEFINITION = {
    "triggers": {"Every_hour": {"type": "Recurrence", "recurrence": {"frequency": "Hour", "interval": 1}}},
    "actions": {
        "Get_orders": {"type": "Http", "runAfter": {}},
        "For_each_order": {"type": "Foreach", "runAfter": {"Get_orders": ["Succeeded"]},
                           "actions": {"Insert_row": {"type": "ApiConnection", "runAfter": {}}}},
        "Check": {"type": "If", "runAfter": {"For_each_order": ["Succeeded"]},
                  "actions": {"Notify": {"type": "ApiConnection"}}, "else": {"actions": {"Log": {"type": "Compose"}}}},
    },
}


def z(dt: datetime) -> str:
    return dt.isoformat() + "Z"


class FakeLogicApps:
    def __init__(self):
        self.runs: list[dict] = []
        self.actions: dict[str, list[dict]] = {}

    def add(self, start: datetime, status: str = "Succeeded", minutes: float = 2):
        name = f"0858{len(self.runs):028d}CU01"
        end = start + timedelta(minutes=minutes)
        err = {"code": "ActionFailed", "message": "An action failed. No dependent actions succeeded."} if status == "Failed" else None
        self.runs.append({"name": name, "properties": {"status": status, "startTime": z(start), "endTime": z(end) if status != "Running" else None,
                                                       "trigger": {"name": "Every_hour"}, **({"error": err} if err else {})}})
        failed = status == "Failed"
        self.actions[name] = [
            {"name": "Get_orders", "properties": {"status": "Succeeded", "startTime": z(start), "endTime": z(start)}},
            {"name": "For_each_order", "properties": {"status": "Failed" if failed else "Succeeded", "startTime": z(start), "endTime": z(end)}},
            {"name": "Insert_row", "properties": {"status": "Failed" if failed else "Succeeded", "startTime": z(start), "endTime": z(end),
                                                  **({"error": {"code": "BadRequest", "message": "Column 'ORDER_ID' cannot be null."}} if failed else {})}},
        ]
        return name

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        path = request.url.path
        assert request.url.params["api-version"] == "2016-06-01"
        if path == LA_BASE:
            return httpx.Response(200, json={"value": [
                {"name": "wf-orders", "properties": {"state": "Enabled", "definition": DEFINITION}},
                {"name": "wf-off", "properties": {"state": "Disabled", "definition": {"actions": {}}}}]})
        if path == f"{LA_BASE}/wf-orders/runs":
            since = request.url.params["$filter"].removeprefix("startTime ge ").rstrip("Z")
            return httpx.Response(200, json={"value": [r for r in self.runs if r["properties"]["startTime"].rstrip("Z") >= since[:19]]
                                             + [{"name": "skipped-1", "properties": {"status": "Skipped", "startTime": z(NOW)}}]})
        if path.startswith(f"{LA_BASE}/wf-orders/runs/") and path.endswith("/actions"):
            return httpx.Response(200, json={"value": self.actions[path.split("/")[-2]]})
        if path.startswith(f"{LA_BASE}/wf-orders/runs/"):
            hit = next((r for r in self.runs if r["name"] == path.rsplit("/", 1)[-1]), None)
            return httpx.Response(200, json=hit) if hit else httpx.Response(404)
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}"}})


@pytest.fixture
def logic(people, monkeypatch):
    from alertbot import connectors
    from alertbot.connectors.logic_apps import LogicAppsPipeline

    fake = FakeLogicApps()
    real = connectors.azure_pipeline

    def build(cfg):
        if cfg.type == "logic_apps":
            return LogicAppsPipeline(cfg, credential=FakeCredential(), transport=httpx.MockTransport(fake.handler))
        return real(cfg)

    monkeypatch.setattr(connectors, "azure_pipeline", build)
    _discover(people["other@contoso.com"], GRAPH_ROWS[2:])
    return fake


def test_logic_apps_runs_steps_and_schedule(logic):
    from alertbot.config import load_projects
    from alertbot.connectors import azure_pipeline

    for h in range(30, 0, -1):
        logic.add(NOW - timedelta(hours=h))
    failed = logic.add(NOW - timedelta(minutes=20), status="Failed")
    la = azure_pipeline(load_projects()["la-rg-la"].pipeline)
    assert la.watched_pipelines() == ["wf-orders"]  # disabled workflows are left out
    sched = la.schedules()["wf-orders"][0]
    assert sched.trigger == "Every_hour" and sched.every == timedelta(hours=1)
    runs = la.query_runs(NOW - timedelta(hours=2), NOW + timedelta(minutes=5))
    assert [r.status for r in runs] == ["Succeeded", "Succeeded", "Failed"]  # the skipped trigger is not a run
    assert runs[-1].invoked_by == "Trigger · Every_hour"
    tree = la.run_tree("wf-orders", failed)
    loop = next(n for n in tree if n["name"] == "For_each_order")
    assert loop["status"] == "Failed" and loop["children"][0]["name"] == "Insert_row"
    assert loop["children"][0]["runs"][0]["error"] == "Column 'ORDER_ID' cannot be null."
    check = next(n for n in tree if n["name"] == "Check")
    assert [(c["name"], c["branch"]) for c in check["children"]] == [("Notify", "If true"), ("Log", "If false")]


def test_logic_app_failure_opens_an_incident(logic):
    from alertbot.config import load_projects
    from alertbot.engine.cycle import backfill, run_cycle

    for h in range(48, 0, -1):
        logic.add(NOW - timedelta(hours=h))
    logic.add(NOW - timedelta(minutes=20), status="Failed")
    cfg = load_projects()["la-rg-la"]
    backfill(cfg, days=3, now=NOW - timedelta(hours=1))
    r = run_cycle(cfg, now=NOW)
    assert r.status == "ok", r.error
    with session_scope() as s:
        inc = s.scalars(select(Incident).where(Incident.scope == "site:wf-orders")).one()
        assert inc.title == "wf-orders pipeline failed"
        assert "Insert_row" in (inc.summary + inc.likely_cause)


# ── Synapse ──────────────────────────────────────────────────────────────────


def test_synapse_reads_the_workspace_endpoint(people):
    from alertbot.config import load_projects
    from alertbot.connectors.synapse import SynapsePipeline

    _discover(people["other@contoso.com"], GRAPH_ROWS[1:2])
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path, request.url.params.get("api-version")))
        if request.url.path == "/queryPipelineRuns":
            return httpx.Response(200, json={"value": [{"runId": "11111111-2222-3333-4444-555555555555", "pipelineName": "PL Load",
                                                        "status": "Succeeded", "runStart": z(NOW), "runEnd": z(NOW), "durationInMs": 1000}]})
        if request.url.path.endswith("/queryActivityruns"):
            return httpx.Response(200, json={"value": []})
        return httpx.Response(404)

    scopes = []

    class Cred:
        def get_token(self, *s, **_):
            scopes.append(s[0])
            return type("T", (), {"token": "t"})()

    syn = SynapsePipeline(load_projects()["syn-analytics-ws"].pipeline, credential=Cred(), transport=httpx.MockTransport(handler))
    runs = syn.query_runs(NOW - timedelta(hours=1), NOW)
    syn.get_activity_runs(runs[0].run_id)
    assert scopes[0] == "https://dev.azuresynapse.net/.default"
    assert seen[0] == ("analytics-ws.dev.azuresynapse.net", "/queryPipelineRuns", "2020-12-01")
    assert seen[1][1] == "/pipelines/PL Load/pipelineruns/11111111-2222-3333-4444-555555555555/queryActivityruns"
    assert "web.azuresynapse.net" in syn.monitor_url(runs[0].run_id)


def test_disabled_logic_apps_are_listed_too(logic, monkeypatch):
    from fastapi.testclient import TestClient

    from alertbot.api.main import app
    from alertbot.config import load_projects
    from alertbot.engine.cycle import run_cycle

    logic.add(NOW - timedelta(hours=1))
    assert run_cycle(load_projects()["la-rg-la"], now=NOW).status == "ok"
    from conftest import freeze_now

    freeze_now(monkeypatch, NOW + timedelta(hours=1))  # the history is built at a fixed date
    items = TestClient(app, headers={"Authorization": "Bearer dev:other@contoso.com"}).get("/api/pipelines").json()["items"]
    by = {i["name"]: i for i in items}
    assert by["wf-off"]["state"] == "disabled" and by["wf-off"]["type"] == "logic_app"
    assert by["wf-orders"]["state"] == "healthy" and by["wf-orders"]["steps"] == 6
    assert by["wf-orders"]["triggers"] == [{"name": "Every_hour", "kind": "Schedule", "started": True, "every_hours": 1.0}]


def test_pipelines_show_the_logic_apps_and_function_apps_they_call(logic):
    import json as _json

    from fastapi.testclient import TestClient

    from alertbot.api.main import app
    from alertbot.config import load_projects
    from alertbot.connectors.adf import connections
    from alertbot.db import BotState
    from alertbot.engine.cycle import run_cycle

    wid = "0123456789abcdef0123456789abcdef"
    acts = [
        {"name": "Notify", "type": "WebActivity", "typeProperties": {
            "url": f"https://prod-12.westeurope.logic.azure.com:443/workflows/{wid}/triggers/manual/paths/invoke?api-version=2016-10-01&sp=x&sig=SECRET123"}},
        {"name": "Loop", "type": "ForEach", "typeProperties": {"activities": [
            {"name": "Score", "type": "AzureFunctionActivity", "linkedServiceName": {"referenceName": "LS_FUNC"},
             "typeProperties": {"functionName": "score-orders"}}]}},
        {"name": "Child", "type": "ExecutePipeline", "typeProperties": {"pipeline": {"referenceName": "PL_CHILD"}}},
    ]
    conns = connections(acts, {"LS_FUNC": {"type": "AzureFunction", "function_app_url": "https://func-orders.azurewebsites.net"}})
    by = {c["kind"]: c for c in conns}
    assert by["logic_app"]["workflow_id"] == wid and by["logic_app"]["steps"] == ["Notify"]
    assert by["function_app"] == {"kind": "function_app", "name": "func-orders", "steps": ["Score"], "function": "score-orders"}
    assert by["pipeline"]["name"] == "PL_CHILD"
    assert "SECRET123" not in _json.dumps(conns) and "sig=" not in _json.dumps(conns)  # the trigger URL's signature is never kept

    # the Logic App the pipeline calls is resolved to the workflow the person can see, both ways
    logic.add(NOW - timedelta(hours=1))
    assert run_cycle(load_projects()["la-rg-la"], now=NOW).status == "ok"
    with session_scope() as s:
        la = s.get(BotState, "inventory:la-rg-la")
        items = _json.loads(_json.dumps(la.value["items"]))  # a copy: in-place edits of a JSON column aren't saved
        items[[i["name"] for i in items].index("wf-orders")]["workflow_id"] = wid  # the fake has no accessEndpoint
        la.value = {**la.value, "items": items}
        s.add(BotState(key="inventory:adfp", value={"items": [{"name": "PL_ORDERS", "type": "pipeline", "state": "Enabled", "connections": conns}]},
                       updated_at=NOW))
        other = s.scalars(select(User).where(User.email == "other@contoso.com")).one()
        adfp = s.scalars(select(Project).where(Project.key == "adfp")).one()
        if s.get(UserProject, (other.id, adfp.id)) is None:
            s.add(UserProject(user_id=other.id, project_id=adfp.id, role="viewer"))
    v = TestClient(app, headers={"Authorization": "Bearer dev:other@contoso.com"}).get("/api/pipelines").json()
    pl = next(i for i in v["items"] if i["name"] == "PL_ORDERS")
    called = next(c for c in pl["connections"] if c["kind"] == "logic_app")
    assert called["name"] == "wf-orders" and called["project"] == "la-rg-la"
    wf = next(i for i in v["items"] if i["name"] == "wf-orders")
    assert wf["called_by"] == [{"name": "PL_ORDERS", "project": "adfp", "project_name": "CSDW Data Factory"}]
