"""Warehouse cost beyond tables: views (judged by their tables' reads), procedures and functions (execution
statistics), Snowflake's views and procedures, and pipelines nobody runs or that run for nothing. Stand-in clients and
made-up runs only; nothing connects anywhere."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_adf import adf_ws  # noqa: F401 - fixture

from alertbot.db import BotState, DbConnection, Project, Run, User, session_scope
from alertbot.dq import storage as S
from alertbot.timeutil import iso

NOW = datetime(2026, 10, 22, 12, 0)
GB = 1024 ** 3
OLD = NOW - timedelta(days=900)


def _state(tables, objects, covered_days=300, usage="dmv", object_usage=None):
    return {"usage": usage, "covered_since": iso(NOW - timedelta(days=covered_days)), "tables": tables, "objects": objects,
            "object_usage": object_usage or {"views": "base_tables", "procedures": "plan_cache", "functions": "plan_cache"}, "deps": {}}


def _t(schema, name, read=None):
    return {"schema": schema, "table": name, "bytes": GB, "rows": 10.0, "created": iso(OLD), "last_read": iso(read) if read else None, "last_write": None}


def _o(schema, name, code, uses=(), used_by=(), last=None):
    kind = S.SQL_TYPES[code][0]
    return {"schema": schema, "name": name, "type": kind, "code": code, "created": iso(OLD), "altered": iso(OLD),
            "last_used": iso(last) if last else None, "uses": list(uses), "used_by": list(used_by)}


def test_views_are_judged_by_their_tables_and_procedures_by_their_runs():
    tables = {"dbo.OLD": _t("dbo", "OLD"), "dbo.HOT": _t("dbo", "HOT", read=NOW - timedelta(days=3))}
    objects = {
        "dbo.v_old": _o("dbo", "v_old", "V", uses=["dbo.OLD"], used_by=["dbo.v_on_v"]),
        "dbo.v_on_v": _o("dbo", "v_on_v", "V", uses=["dbo.v_old"]),
        "dbo.v_hot": _o("dbo", "v_hot", "V", uses=["dbo.HOT", "dbo.OLD"]),
        "dbo.v_remote": _o("dbo", "v_remote", "V", uses=["otherdb.dbo.X"]),
        "dbo.p_idle": _o("dbo", "p_idle", "P"),
        "dbo.p_busy": _o("dbo", "p_busy", "P", last=NOW - timedelta(hours=5)),
        "dbo.fn_inline": _o("dbo", "fn_inline", "IF", uses=["dbo.HOT"]),
    }
    st = _state(tables, objects)
    c = {k: S.classify_object(o, st, "sqlserver", NOW, 180, [], None) for k, o in objects.items()}
    assert c["dbo.v_old"]["status"] == "unused" and c["dbo.v_old"]["confidence"] == "high" and "can't have been queried" in c["dbo.v_old"]["why"]
    assert c["dbo.v_on_v"]["status"] == "unused"  # a view on an unread view: through to its tables
    assert c["dbo.v_hot"]["status"] == "in_use" and "tables were read 3 days ago" in c["dbo.v_hot"]["why"]
    assert c["dbo.v_remote"]["status"] == "unknown" and "another database" in c["dbo.v_remote"]["why"]
    assert c["dbo.p_idle"]["status"] == "unused" and c["dbo.p_idle"]["confidence"] == "medium" and "Never run" in c["dbo.p_idle"]["why"]
    assert c["dbo.p_busy"]["status"] == "in_use"
    assert c["dbo.fn_inline"]["status"] == "in_use"  # inline table functions are read through their tables, like views
    assert S.classify_object(objects["dbo.p_idle"], st, "sqlserver", NOW, 180, ["pipeline pl_load"], None)["status"] == "in_use"
    young = _state(tables, objects, covered_days=20)
    assert S.classify_object(objects["dbo.p_idle"], young, "sqlserver", NOW, 180, [], None)["status"] == "watching"
    no_stats = _state(tables, objects, object_usage={"views": None, "procedures": None, "functions": None})
    assert S.classify_object(objects["dbo.p_idle"], no_stats, "sqlserver", NOW, 180, [], None)["status"] == "unknown"


def test_script_drops_callers_first_and_warns_about_what_breaks():
    conn = DbConnection(name="TT", kind="sqlserver", host="203.0.113.5", database="TeamTesseract")
    tables = {"dbo.OLD": _t("dbo", "OLD")}
    objects = {"dbo.v_old": _o("dbo", "v_old", "V", uses=["dbo.OLD"], used_by=["dbo.p_report"]),
               "dbo.p_report": _o("dbo", "p_report", "P", uses=["dbo.v_old"]), "dbo.fn_x": _o("dbo", "fn_x", "FN")}
    st = _state(tables, objects)
    st["deps"] = {"dbo.OLD": [{"object": "dbo.v_old", "kind": "view or procedure"}]}
    s = S.script(conn, st, ["dbo.OLD", "dbo.v_old", "dbo.fn_x"], "drop", "Jnana", NOW)
    body = s["sql"].split("BEGIN TRANSACTION;")[1]
    assert body.index("DROP FUNCTION [dbo].[fn_x]") < body.index("DROP VIEW [dbo].[v_old]") < body.index("DROP TABLE [dbo].[OLD]")
    assert "IF OBJECT_ID(N'dbo.fn_x', N'FN') IS NOT NULL" in body and s["items"] == 3 and s["tables"] == 1 and s["objects"] == 2
    assert any("used by procedure dbo.p_report" in w for w in s["warnings"])  # p_report wasn't picked: it would break
    assert not any("dbo.OLD is referenced by" in w for w in s["warnings"])  # its view goes too
    assert "1 function, 1 view and 1 table" in s["sql"]
    a = S.script(conn, st, ["dbo.v_old"], "archive", "Jnana", NOW)["sql"]
    assert "IF OBJECT_ID(N'dbo.v_old', N'V') IS NOT NULL ALTER SCHEMA [archive] TRANSFER [dbo].[v_old];" in a


class FakeSnowflake:
    def meta(self, mq, limit=5000):
        old = NOW - timedelta(days=500)
        return {
            "storage_sizes": [("RAW", "EVENTS", old, old, 10, GB, old, "NO", 1)],
            "storage_reads": [("DB_PROD.RAW.EVENTS", NOW - timedelta(days=400))],
            "storage_writes": [],
            "storage_views": [("RAW", "V_EVENTS", old, old), ("RAW", "V_LIVE", old, old)],
            "storage_procs": [("RAW", "LOAD_EVENTS", "(SRC VARCHAR, N NUMBER)", old, old), ("RAW", "LOAD_EVENTS", "()", old, old),
                              ("RAW", "NIGHTLY", "(D DATE)", old, old)],
            "storage_funcs": [("RAW", "CLEAN", "(S VARCHAR)", old, old)],
            "storage_view_reads": [("DB_PROD.RAW.V_LIVE", NOW - timedelta(days=2))],
            "storage_calls": [("DB_PROD.RAW.NIGHTLY", NOW - timedelta(days=1)), ('"DB_PROD"."RAW"."LOAD_EVENTS"', NOW - timedelta(days=300))],
        }[mq.name]


def test_snowflake_views_procedures_and_their_overloads():
    conn = SimpleNamespace(kind="snowflake", name="SF", database="DB_PROD", host="acct")
    got = S.read(FakeSnowflake(), conn)
    assert got["object_usage"] == {"views": "access_history", "procedures": "query_history", "functions": None}
    assert got["objects"]["RAW.LOAD_EVENTS"]["signatures"] == ["VARCHAR, NUMBER", ""]
    st = S.merge(None, got, NOW)
    c = {k: S.classify_object(o, st, "snowflake", NOW, 180, [], None)["status"] for k, o in st["objects"].items()}
    assert c == {"RAW.V_EVENTS": "unused", "RAW.V_LIVE": "in_use", "RAW.LOAD_EVENTS": "unused", "RAW.NIGHTLY": "in_use", "RAW.CLEAN": "unknown"}
    sql = S.script(SimpleNamespace(**vars(conn)), st, ["RAW.LOAD_EVENTS", "RAW.V_EVENTS"], "drop", "Jnana", NOW)["sql"]
    assert 'DROP PROCEDURE IF EXISTS "DB_PROD"."RAW"."LOAD_EVENTS"(VARCHAR, NUMBER);' in sql
    assert 'DROP PROCEDURE IF EXISTS "DB_PROD"."RAW"."LOAD_EVENTS"();' in sql and 'DROP VIEW IF EXISTS "DB_PROD"."RAW"."V_EVENTS";' in sql
    assert S._arg_types("(A VARCHAR; DROP TABLE X)") is None  # anything odd: no script line for it


INVENTORY = {"items": [
    {"name": "pl_orphan", "type": "pipeline", "steps": 2, "triggers": [], "connections": [], "lineage": []},
    {"name": "pl_child", "type": "pipeline", "steps": 1, "triggers": [], "connections": [], "lineage": []},
    {"name": "pl_parent", "type": "pipeline", "steps": 3, "triggers": [{"name": "tr_daily", "kind": "Schedule", "started": True}],
     "connections": [{"kind": "pipeline", "name": "pl_child", "steps": ["Run child"]},
                     {"kind": "procedure", "name": "[dbo].[usp_refresh]", "steps": ["Refresh"]}], "lineage": []},
    {"name": "pl_staging", "type": "pipeline", "steps": 1, "triggers": [{"name": "tr_hourly", "kind": "Schedule", "started": True}], "connections": [],
     "lineage": [{"step": "Copy", "type": "Copy", "reads": [], "writes": [{"kind": "table", "platform": "SQL Server", "host": "203.0.113.9",
                                                                          "table": "TEAMTESSERACT.DBO.STAGING_DUMP"}]}]},
]}


class FakeSqlServer:
    def __init__(self, now_local):
        self.now = now_local

    def meta(self, mq, limit=5000):
        old = self.now - timedelta(days=700)
        return {
            "storage_clock": [(0,)],
            "storage_start": [(self.now - timedelta(days=250),)],
            "storage_sizes": [("dbo", "STAGING_DUMP", old, old, 5_000_000, 8 * GB)],
            "storage_usage": [("dbo", "STAGING_DUMP", None, self.now - timedelta(hours=1))],  # loaded hourly, never read
            "storage_objects": [("dbo", "usp_refresh", "P", old, old), ("dbo", "usp_dead", "P", old, old)],
        }.get(mq.name, [])


def test_pipelines_unused_wasted_called_and_their_script(adf_ws, monkeypatch):  # noqa: F811
    from alertbot.api.main import app
    from alertbot.dq import service

    now = datetime.now().replace(microsecond=0)
    monkeypatch.setattr(service, "client_of", lambda conn: FakeSqlServer(now))
    with session_scope() as s:
        user = s.query(User).filter_by(email="chinmoy@contoso.com").one()
        s.add(DbConnection(user_id=user.id, name="TeamTesseract", kind="sqlserver", host="203.0.113.9", port=1433, database="TeamTesseract",
                           auth="sql", username="reader", status="ok"))
        p = s.query(Project).filter_by(key="adfp").one()
        p.created_at = now - timedelta(days=400)  # watched long enough to be sure
        s.add(BotState(key="inventory:adfp", value=INVENTORY))
        for i in range(60):  # pl_staging: every hour; pl_parent: daily
            at = now - timedelta(hours=i + 1)
            s.add(Run(project_id=p.id, trigger_id=f"s{i}", site="pl_staging", trigger_status="succeeded", run_id=f"rs{i}", pipeline="Schedule · tr_hourly",
                      run_status="Succeeded", requested_at=at, started_at=at, duration_s=600))
        for i in range(3):
            at = now - timedelta(days=i + 1)
            s.add(Run(project_id=p.id, trigger_id=f"p{i}", site="pl_parent", trigger_status="succeeded", run_id=f"rp{i}", pipeline="Schedule · tr_daily",
                      run_status="Succeeded", requested_at=at, started_at=at, duration_s=120))
    api = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com", "X-AlertBot": "1"})
    r = api.post("/api/storage/scan?days=180").json()
    db = r["databases"][0]
    objs = {o["key"]: o for o in db["objects"]}
    assert objs["dbo.usp_refresh"]["status"] == "in_use" and "pipeline pl_parent" in objs["dbo.usp_refresh"]["why"]  # a pipeline calls it
    assert objs["dbo.usp_dead"]["status"] == "unused" and r["summary"]["unused_procedures"] == 1
    assert {t["key"]: t["status"] for t in db["tables"]}["dbo.STAGING_DUMP"] == "in_use"  # a pipeline loads it: the table is protected

    rows = {x["name"]: x for x in r["pipelines"]["projects"][0]["rows"]}
    assert rows["pl_orphan"]["status"] == "unused" and "nothing starts it" in rows["pl_orphan"]["why"] and rows["pl_orphan"]["confidence"] == "high"
    assert rows["pl_child"]["status"] == "in_use" and "Called by pl_parent" in rows["pl_child"]["why"]
    assert rows["pl_parent"]["status"] == "in_use"
    # pl_staging loads a table nobody reads: protected from deletion, but its runs are flagged as wasted
    st = rows["pl_staging"]
    assert st["status"] == "wasted" and "nobody reads what it loads (STAGING_DUMP)" in st["why"]
    assert st["runs_30d"] == 60 and st["hours_30d"] == 10.0
    assert st["saving"] == round(60 * 1 * 0.001 + 10.0 * 4 * 0.25, 2) == r["pipelines"]["summary"]["monthly"]  # $1/1,000 activities + 4 DIU × $0.25/h
    assert r["summary"]["all_monthly"] == round(r["summary"]["monthly"] + st["saving"], 2)

    sc = api.post("/api/storage/pipelines/adfp/script", json={"names": ["pl_orphan", "pl_child"], "mode": "delete"}).json()
    sql = sc["sql"]
    assert 'az account set --subscription "sub-1"' in sql
    assert 'az datafactory pipeline show --resource-group "rg-1" --factory-name "dev-factory" --name "pl_orphan" > "pl_orphan.json"' in sql
    assert sql.index("pipeline show") < sql.index("pipeline delete") and 'pipeline delete --resource-group "rg-1" --factory-name "dev-factory" --name "pl_orphan" --yes' in sql
    assert any("pl_child: in use" in x for x in sc["skipped"]) and sc["items"] == 1
    stop = api.post("/api/storage/pipelines/adfp/script", json={"names": ["pl_orphan"], "mode": "stop"}).json()["sql"]
    assert "pipeline delete" not in stop and "STOP ONLY" in stop
    assert api.put("/api/storage/pipelines/adfp/keep", json={"name": "pl_orphan", "reason": "quarterly"}).status_code == 200
    again = {x["name"]: x for x in api.get("/api/storage").json()["pipelines"]["projects"][0]["rows"]}
    assert again["pl_orphan"]["status"] == "kept" and "quarterly" in again["pl_orphan"]["why"]
    other = TestClient(app, headers={"Authorization": "Bearer dev:other@contoso.com", "X-AlertBot": "1"})
    assert other.post("/api/storage/pipelines/adfp/script", json={"names": ["pl_orphan"]}).status_code == 404
    assert other.get("/api/storage").json()["pipelines"]["projects"] == []
