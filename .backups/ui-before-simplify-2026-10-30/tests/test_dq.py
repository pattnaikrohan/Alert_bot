"""Data quality on databases people connect, aggregate-only. One test per security requirement:

    no production rows reach the AI           test_a_raw_row_from_the_connector_is_rejected_before_the_ai
    only approved aggregates reach the AI     test_end_to_end_mapping_checks_and_the_ai_payload
    the AI can't run SQL                      test_the_ai_has_no_tools_and_no_sql
    credentials are protected                 test_credentials_are_encrypted_and_never_returned_or_logged
    SQL injection is prevented                test_identifiers_are_allowlisted_and_values_are_bound
    read-only, even with a write-capable login test_only_single_selects_run_and_nothing_is_committed
    concurrent runs don't mix                 test_concurrent_runs_keep_their_own_results
    logs and errors don't leak data           test_errors_become_our_own_sentences_and_logs_stay_clean
    resource limits                           test_sql_server_connections_are_limited_and_encrypted
    a failed check can't modify data          test_only_single_selects_run_and_nothing_is_committed

A SQLite file stands in for SQL Server / Snowflake (the same templates, its own dialect); the T-SQL text is checked too.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_adf import NOW, adf_ws  # noqa: F401 - adf_ws is a fixture

from alertbot.db import BotState, DbConnection, DqMapping, Project, User, session_scope

SECRET_VALUE = "SECRET-ROW-VALUE"
SECRET_EMAIL = "jane.doe@corp.example"
PASSWORD = "Sup3r-S3cret!pw"
TODAY = NOW.replace(hour=9, minute=0, second=0, microsecond=0)


def _source(path, table="NEWS_ARTICLES2", rows=50):
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE {table} (ID INTEGER, TITLE TEXT, AUTHOR_EMAIL TEXT, AMOUNT NUMERIC, PRICE_TEXT TEXT, STATUS TEXT, LOAD_DATE TIMESTAMP)")
    at = TODAY.isoformat(sep=" ")
    data = []
    for i in range(rows):
        data.append((None if i == 0 else i, None if i in (1, 2, 3, 4) else f"{SECRET_VALUE} {i}", SECRET_EMAIL,
                     -5 if i in (5, 6) else (5000 if i == 7 else 10 + i), "PRICE-NOT-NUMBER" if i < 5 else f"{i}.50", ["A", "B", "C"][i % 3], at))
    data += [(10, SECRET_VALUE, SECRET_EMAIL, 20, "1.0", "A", at), (11, SECRET_VALUE, SECRET_EMAIL, 21, "1.0", "A", at), (11, SECRET_VALUE, SECRET_EMAIL, 21, "1.0", "A", at)]
    db.executemany(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?)", data)
    db.execute(f"INSERT INTO {table} VALUES (999, 'old', NULL, 1, '1', 'A', '2026-01-01 00:00:00')")
    db.commit()
    db.close()


SETTINGS = {"key": ["ID"], "sensitive": ["AUTHOR_EMAIL"], "stats": ["AMOUNT"], "required": ["TITLE"],
            "ranges": [{"column": "AMOUNT", "min": 0, "max": 1000}], "typed": [{"column": "PRICE_TEXT", "type": "decimal"}],
            "rules": [{"name": "amount is positive", "column": "AMOUNT", "op": ">", "values": [0]}]}


@pytest.fixture
def dq(adf_ws, tmp_path, monkeypatch):  # noqa: F811
    from alertbot.dq import client as dqclient
    from alertbot.dq import vault

    vault._fernet.cache_clear()
    monkeypatch.setenv("ALERTBOT_SECRET_KEY_FILE", str(tmp_path / "secret.key"))
    from conftest import _clear_caches

    _clear_caches()
    vault._fernet.cache_clear()
    _source(tmp_path / "src.db")
    with session_scope() as s:
        s.add(BotState(key="inventory:adfp", value={"items": [{"name": "pl_sql", "lineage": []}, {"name": "pl_other", "lineage": []}]}))
    api = TestClient(__import__("alertbot.api.main", fromlist=["app"]).app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com"})
    yield SimpleNamespace(api=api, path=tmp_path / "src.db", tmp=tmp_path)
    for cid in list(dqclient._ENGINES):
        dqclient.forget(cid)
    vault._fernet.cache_clear()


def _connect(dq, path=None):
    r = dq.api.post("/api/databases", json={"kind": "sqlite", "host": str(path or dq.path), "database": "main", "auth": "sql",
                                            "name": "Team Tesseract"})
    assert r.status_code == 200, r.text
    return r.json()["connection"]["id"]


def _map(dq, conn_id, table="news_articles2", settings=None, pipeline="pl_sql"):
    r = dq.api.post("/api/projects/adfp/mappings", json={"pipeline": pipeline, "connection_id": conn_id, "schema": "main", "table": table,
                                                         "settings": SETTINGS if settings is None else settings})
    assert r.status_code == 200, r.text
    return r.json()["mapping"]["id"]


def _ctx(s, now=TODAY + timedelta(hours=1)):
    from alertbot.config import load_projects
    from alertbot.engine.cycle import make_context

    return make_context(s, load_projects()["adfp"], now)


class SpyAI:
    """Stands in for the model: records exactly what it was sent."""

    def __init__(self):
        self.sent: list[str] = []
        self.users: list[str] = []

    def chat_json(self, system, user, max_tokens=0):
        self.sent.append(system + "\n" + user)
        self.users.append(user)
        return {"summary": "Looks like a partial load.", "tables": [{"table": "main.NEWS_ARTICLES2", "assessment": "ok",
                                                                    "likely_causes": ["late file"], "next_steps": ["re-run"]}]}, SimpleNamespace(model="spy")


@pytest.fixture
def spy(monkeypatch):
    s = SpyAI()
    monkeypatch.setattr("alertbot.ai.llm.client", lambda: s)
    return s


def _collect(ids):
    from alertbot.dq.graph import run_dq

    with session_scope() as s:
        return run_dq(_ctx(s), ids, stage="collect")


def test_end_to_end_mapping_checks_and_the_ai_payload(dq, spy, caplog):
    caplog.set_level(logging.DEBUG)
    conn = _connect(dq)
    tables = dq.api.get(f"/api/databases/{conn}/tables").json()["tables"]
    assert {"schema": "main", "table": "NEWS_ARTICLES2", "kind": "table"} in tables
    cols = dq.api.get(f"/api/databases/{conn}/columns", params={"schema": "MAIN", "table": "news_articles2"}).json()
    assert cols["table"] == "NEWS_ARTICLES2" and {"name": "AMOUNT", "type": "NUMERIC", "kind": "number"} in cols["columns"]

    # a column that isn't in the table can't be configured (the allowlist is the table's own metadata)
    bad = dq.api.post("/api/projects/adfp/mappings", json={"pipeline": "pl_sql", "connection_id": conn, "schema": "main",
                                                          "table": "NEWS_ARTICLES2", "settings": {"key": ["NOPE"]}})
    assert bad.status_code == 422 and "NOPE" in bad.text
    mid = _map(dq, conn)
    listed = dq.api.get("/api/projects/adfp/mappings").json()
    assert listed["mappings"][0]["object"] == "MAIN.MAIN.NEWS_ARTICLES2" and listed["pipelines"] == ["pl_other", "pl_sql"]

    state = _collect([mid])
    assert not state["errors"], state["errors"]
    p = state["profiled"][0].profile
    assert p.profiled_rows == 53 and p.rows_total == 54           # today's rows (by LOAD_DATE) vs the whole table
    assert p.nulls["TITLE"] == 4 and p.nulls["AUTHOR_EMAIL"] == 0  # sensitive columns are still counted as empty / not
    assert p.duplicate_rows == 3 and p.duplicate_groups == 2 and p.null_key_rows == 1  # the NULL id is not a duplicate
    assert p.rule_fails == {"amount is positive": 2} and p.out_of_range == {"AMOUNT": 3} and p.invalid == {"PRICE_TEXT": 5}
    assert set(p.stats["AMOUNT"]) == {"min", "max", "avg", "stddev"} and p.stats["AMOUNT"]["min"] == -5
    assert "AUTHOR_EMAIL" not in p.means and "AUTHOR_EMAIL" not in p.top_share  # sensitive: no statistics at all
    assert p.top_share and all(isinstance(v, float) for v in p.top_share.values())  # shares, never the values

    # the bot's judging: a full cycle-style judge of the batch opens the failing checks
    from alertbot.engine.quality import judge_quality

    with session_scope() as s:
        from alertbot.dq.service import collect_mapped

        ctx = _ctx(s)
        batch = collect_mapped(ctx, force=True)
        outcomes = judge_quality(ctx, batch)
    failing = {o.check for o in outcomes if o.status == "fail"}
    assert {"business_rule", "out_of_range", "invalid_type", "duplicates"} <= failing

    # the analyse stage: the model gets numbers and names only
    from alertbot.db import BotCycle, CheckResult
    from alertbot.dq.graph import run_dq

    with session_scope() as s:
        ctx = _ctx(s)
        cyc = BotCycle(project_id=ctx.project.id, started_at=ctx.now, status="ok")
        s.add(cyc)
        s.flush()
        for o in outcomes:
            s.add(CheckResult(cycle_id=cyc.id, project_id=ctx.project.id, check=o.check, scope=o.scope, status=o.status, severity=o.severity,
                              message=o.message, details=o.details, created_at=ctx.now))
        s.flush()
        rep = run_dq(ctx, [mid], stage="analyze")["report"]
    assert rep["tables"][0]["quality_status"] in ("WARNING", "CRITICAL") and rep["analysis"]["summary"]
    sent = "\n".join(spy.sent)
    payload = json.loads(spy.users[0])
    for t in payload["tables"]:
        assert all(v is None or isinstance(v, (int, float)) for v in t["quality_metrics"].values())
    for leaked in (SECRET_VALUE, SECRET_EMAIL, "PRICE-NOT-NUMBER"):
        assert leaked not in sent
        assert leaked not in caplog.text
    q = dq.api.get("/api/projects/adfp/quality").json()
    assert q["enabled"] and q["mappings"][0]["id"] == mid and q["report"]["run_id"].startswith("dq-")


def test_a_raw_row_from_the_connector_is_rejected_before_the_ai(dq, spy, monkeypatch, caplog):
    """The connector 'accidentally' returns a production row instead of an aggregate. It must be refused at the gate:
    the table's check fails closed, nothing from it is kept, logged, judged or sent to the model."""
    caplog.set_level(logging.DEBUG)
    conn = _connect(dq)
    mid = _map(dq, conn)
    from alertbot.dq.client import DbClient

    real = DbClient._run

    def leaky(self, name, sql, params, max_rows):
        if name == "profile":
            return ["TITLE", "AUTHOR_EMAIL"], [(f"{SECRET_VALUE} 1", SECRET_EMAIL)]
        return real(self, name, sql, params, max_rows)

    monkeypatch.setattr(DbClient, "_run", leaky)
    state = _collect([mid])
    assert state["profiled"] == [] and state["tables"][mid]["status"] == "error"
    assert "wasn't an approved aggregate" in state["tables"][mid]["error"]
    from alertbot.dq.graph import run_dq

    with session_scope() as s:
        run_dq(_ctx(s), [mid], stage="analyze")
    everything = json.dumps({k: v for k, v in state.items() if k != "profiled"}, default=str) + "\n".join(spy.sent) + caplog.text
    assert SECRET_VALUE not in everything and SECRET_EMAIL not in everything

    # the same gate, directly: one row of named numbers, or nothing
    from decimal import Decimal

    from alertbot.dq.privacy import RawDataRejected, Spec, validate_aggregate

    spec = Spec((("m0", "count"), ("m1", "number")))
    assert validate_aggregate([(3, Decimal("2.5"))], ["m0", "m1"], spec) == {"m0": 3, "m1": 2.5}
    for rows, keys in (([("John", 1)], ["m0", "m1"]), ([(1, 2), (3, 4)], ["m0", "m1"]), ([(1, 2)], ["NAME", "m1"]),
                       ([(1, 2, 3)], ["m0", "m1", "m2"]), ([(True, 1)], ["m0", "m1"]), ([(b"\x00", 1)], ["m0", "m1"]), ([(-1, 1)], ["m0", "m1"])):
        with pytest.raises(RawDataRejected):
            validate_aggregate(rows, keys, spec)

    # and the AI payload model itself refuses anything but numbers
    from alertbot.dq.privacy import AiPayload

    with pytest.raises(ValueError):
        AiPayload.model_validate({"run_id": "dq-1", "project": "adfp", "tables": [{
            "database": "d", "schema": "s", "table": "t", "pipeline": "p", "quality_status": "OK",
            "quality_metrics": {"top_value": "John Smith"}}]})


def test_the_ai_has_no_tools_and_no_sql():
    """The checks are LangChain tools with no inputs at all (no SQL, no table, no column); the workflow calls them,
    and the analysis node only ever sends the validated payload. No model is bound to any tool."""
    import inspect

    from alertbot.dq import graph, tools

    src = inspect.getsource(graph) + inspect.getsource(tools)
    assert "bind_tools" not in src and "SQLDatabaseToolkit" not in src and "create_sql_agent" not in src
    from alertbot.dq.service import TableRun
    from alertbot.dq.sql import DIALECTS, Plan

    run = TableRun(client=SimpleNamespace(dialect=DIALECTS["sqlite"]), plan=Plan(schema="main", table="T", columns={}), start=NOW)
    made = tools.make_tools(run, NOW, None)
    assert set(made) >= {"check_row_count", "check_null_percentage", "check_duplicate_count", "check_data_freshness",
                         "check_schema_changes", "check_invalid_datatypes", "check_business_rule_violations",
                         "check_referential_integrity"}
    for t in made.values():
        assert t.args == {}  # nothing a model could put SQL or a name into
    # whatever is passed in is dropped: the tool reads only what was fixed when the table was mapped
    schema_tool = made["check_schema_changes"]
    assert schema_tool.invoke({"sql": "DROP TABLE T", "table": "OTHER"}) == schema_tool.invoke({}) == {"column_count": 0}


def test_identifiers_are_allowlisted_and_values_are_bound(dq):
    from alertbot.dq.privacy import RawDataRejected, safe_name
    from alertbot.dq.sql import DIALECTS, Plan, duplicates, profile

    for bad in ("x]; DROP TABLE y--", 'a"b', "a;b", "a'b", "line\nbreak", ""):
        with pytest.raises(RawDataRejected):
            safe_name(bad)
    conn = _connect(dq)
    r = dq.api.post("/api/projects/adfp/mappings", json={"pipeline": "pl_sql", "connection_id": conn, "schema": "main",
                                                        "table": "NEWS_ARTICLES2", "settings": {"key": ["ID]; DROP TABLE NEWS_ARTICLES2--"]}})
    assert r.status_code == 422
    r = dq.api.post("/api/projects/adfp/mappings", json={"pipeline": "pl_sql", "connection_id": conn, "schema": "main",
                                                        "table": "NEWS_ARTICLES2; DROP TABLE X", "settings": {}})
    assert r.status_code in (404, 422, 502)
    # a rule's value is a bound parameter, never part of the SQL text
    d = DIALECTS["sqlserver"]
    plan = Plan(schema="Alert_bot", table="NEWS_ARTICLES2", columns={"ID": "number", "TITLE": "text", "AMOUNT": "number", "PRICE": "text"},
                date_column=None, key=["ID"], watched=["ID", "TITLE", "AMOUNT", "PRICE"], numeric=["AMOUNT"],
                rules=[("r", "TITLE", "in", ["x'); DROP TABLE t; --", "y"])], typed=[("PRICE", "decimal")], ranges=[("AMOUNT", 0, 1000)])
    q, _ = profile(d, plan, {"start": NOW})
    assert "DROP" not in q.sql and q.params["r0_0"] == "x'); DROP TABLE t; --"
    assert "COUNT_BIG(*)" in q.sql and q.sql.endswith("OPTION (MAXDOP 1)") and "[Alert_bot].[NEWS_ARTICLES2]" in q.sql
    assert "TRY_CONVERT(DECIMAL(38, 10), [PRICE])" in q.sql and ":lo0" in q.sql and ":hi0" in q.sql
    dup = duplicates(d, plan, {})
    assert "[ID] IS NOT NULL" in dup.sql and "HAVING COUNT_BIG(*) > 1" in dup.sql  # empty keys aren't duplicates


def test_only_single_selects_run_and_nothing_is_committed(dq, monkeypatch):
    from alertbot.dq import sql as T
    from alertbot.dq.client import DbClient
    from alertbot.dq.sql import UnsafeStatement, assert_read_only

    for bad in ("DELETE FROM t", "SELECT 1; DROP TABLE t", "SELECT * INTO x FROM t", "WITH a AS (SELECT 1) DELETE FROM t",
                "EXEC sp_who", "SELECT 1 -- hi", "UPDATE t SET a = 1", "SELECT 1 /* x */", "MERGE t USING s ON 1=1",
                "SELECT * FROM OPENROWSET('x')", "TRUNCATE TABLE t", "SELECT xp_cmdshell('dir')"):
        with pytest.raises(UnsafeStatement):
            assert_read_only(bad)
    assert_read_only("SELECT COUNT(*) AS m0 FROM [DELETE].[UPDATE] WHERE [x] = 'DROP TABLE'")  # names and literals are fine

    conn = _connect(dq)
    with session_scope() as s:
        from alertbot.dq.service import client_of

        client = client_of(s.get(DbConnection, conn))
    # a template that tried to sneak a write in is refused before it reaches the database …
    monkeypatch.setattr(T, "rows_total", lambda d, p: T.Query("rows_total", f"SELECT COUNT(*) AS m0 FROM {d.fq(p.schema, p.table)}; DELETE FROM NEWS_ARTICLES2",
                                                             {}, T.Spec((("m0", "count"),))))
    plan = T.Plan(schema="main", table="NEWS_ARTICLES2", columns={})
    with pytest.raises(UnsafeStatement):
        client.aggregate(T.rows_total(client.dialect, plan))
    # … and even a write that got past the guard would be rolled back: every query runs in a transaction never committed
    monkeypatch.setattr(T, "assert_read_only", lambda sql: None)
    with contextlib.suppress(Exception):  # sqlite may or may not complain; what matters is the data below
        client._run("probe", "DELETE FROM NEWS_ARTICLES2", {}, 1)
    assert sqlite3.connect(dq.path).execute("SELECT COUNT(*) FROM NEWS_ARTICLES2").fetchone()[0] == 54
    assert isinstance(client, DbClient)


def test_concurrent_runs_keep_their_own_results(dq):
    other = dq.tmp / "other.db"
    _source(other, table="ORDERS", rows=30)
    a = _connect(dq)
    b = _connect(dq, other)
    ma = _map(dq, a, settings={"key": ["ID"]})
    mb = _map(dq, b, table="ORDERS", settings={"key": ["ID"]}, pipeline="pl_other")
    results: dict[str, object] = {}

    def go(name, mid):
        for _ in range(3):
            results[name] = _collect([mid])

    threads = [threading.Thread(target=go, args=("a", ma)), threading.Thread(target=go, args=("b", mb))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pa, pb = results["a"]["profiled"], results["b"]["profiled"]
    assert [x.profile.table for x in pa] == ["MAIN.MAIN.NEWS_ARTICLES2"] and [x.profile.table for x in pb] == ["MAIN.MAIN.ORDERS"]
    assert pa[0].profile.profiled_rows == 53 and pb[0].profile.profiled_rows == 33
    assert results["a"]["run_id"] != results["b"]["run_id"]


def test_credentials_are_encrypted_and_never_returned_or_logged(dq, caplog):
    caplog.set_level(logging.DEBUG)
    from alertbot.dq.service import info_of, save_connection

    with session_scope() as s:
        user = s.query(User).filter_by(email="chinmoy@contoso.com").one()
        c = save_connection(s, user, {"name": "prod", "kind": "sqlserver", "host": "203.0.113.11", "port": 1433, "database": "TeamTesseract",
                                      "auth": "sql", "username": "jnana", "secret": PASSWORD, "options": {}})
        c.status = "ok"
        cid = c.id
        assert PASSWORD not in c.secret_enc and info_of(c).secret == PASSWORD
        assert PASSWORD not in repr(info_of(c))
    body = dq.api.get("/api/databases").text
    assert PASSWORD not in body and "secret" not in json.loads(body)["connections"][0]
    raw = sqlite3.connect(dq.tmp / "alertbot.db").execute("SELECT secret_enc FROM db_connections WHERE id = ?", (cid,)).fetchone()[0]
    assert PASSWORD not in raw
    assert PASSWORD not in caplog.text
    # someone else can't see or use it
    other = TestClient(dq.api.app, headers={"Authorization": "Bearer dev:other@contoso.com"})
    assert other.get(f"/api/databases/{cid}/tables").status_code == 404
    assert other.get("/api/databases").json()["connections"] == []


def test_sql_server_connections_are_limited_and_encrypted(monkeypatch):
    import pyodbc

    from alertbot.dq import client as C

    seen: dict[str, object] = {}

    class FakeConn:
        timeout = 0

        def cursor(self):
            return SimpleNamespace(execute=lambda sql: seen.setdefault("set", []).append(sql), close=lambda: None)

    def connect(conn_str, **_):
        seen["conn_str"] = conn_str
        return FakeConn()

    monkeypatch.setattr(pyodbc, "connect", connect)
    monkeypatch.setattr(C, "_sqlserver_driver", lambda: "ODBC Driver 18 for SQL Server")
    info = C.ConnInfo(id=1, kind="sqlserver", host="203.0.113.11", port=1433, database="TeamTesseract", auth="sql", username="u", secret=PASSWORD)
    conn = C.open_sqlserver(info, 45)
    assert conn.timeout == 45
    s = seen["conn_str"]
    assert "Encrypt=yes" in s and "TrustServerCertificate=no" in s and "ApplicationIntent=ReadOnly" in s and "Connection Timeout=15" in s
    assert seen["set"] == ["SET LOCK_TIMEOUT 10000", "SET DEADLOCK_PRIORITY LOW", "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED"]
    assert C._global()._value <= 32  # a bound on concurrent queries exists


def test_errors_become_our_own_sentences_and_logs_stay_clean():
    from alertbot.dq.privacy import sanitize_error, scrub_for_llm, scrub_messages

    e = sanitize_error(Exception("('22018', \"[22018] [Microsoft][ODBC Driver 18] Conversion failed when converting the varchar value "
                                 "'John Smith' to data type int. (245)\")"))
    assert "John" not in e.message and e.code == "error"
    assert sanitize_error(Exception("[28000] Login failed for user 'jnana'. (18456)")).code == "login_failed"
    assert sanitize_error(Exception("HYT00 Query timeout expired")).code == "timeout"
    assert "jnana" not in sanitize_error(Exception("[28000] Login failed for user 'jnana'. (18456)")).message
    text = ("Violation of PRIMARY KEY constraint 'PK_T'. Cannot insert duplicate key in object 'dbo.T'. The duplicate key value is (48213). "
            "Conversion failed when converting the varchar value 'John Smith' to data type int. Contact jane.doe@corp.example. "
            'Lookup output: "firstRow": {"NAME": "John Smith", "SALARY": 99000}')
    clean = scrub_for_llm(text)
    for leaked in ("48213", "John Smith", "jane.doe@corp.example", "99000"):
        assert leaked not in clean
    assert "dbo.T" in clean and "PRIMARY KEY" in clean  # what went wrong still reads
    assert "John" not in scrub_messages([{"role": "user", "content": text}])[0]["content"]


def test_activity_outputs_reach_the_ai_as_counts_only(adf_ws):  # noqa: F811
    """A Lookup step's output holds real rows: the investigation tools give a model its row COUNT, never the rows."""
    from alertbot.ai.tools import ToolBox
    from alertbot.connectors.base import ActivityRun

    act = ActivityRun(run_id="r1", name="LookupCustomers", type="Lookup", status="Succeeded", started_at=NOW, ended_at=NOW, duration_s=1,
                      output={"count": 1, "firstRow": {"NAME": "John Smith", "SALARY": 99000}}, input={"source": {"query": "SELECT *"}})
    with session_scope() as s:
        project = s.query(Project).filter_by(key="adfp").one()
        ctx = SimpleNamespace(connectors=SimpleNamespace(pipeline=SimpleNamespace(get_activity_runs=lambda rid: [act])), project=project,
                              session=s, now=NOW, key="adfp")
        box = ToolBox.__new__(ToolBox)
        box.ctx = ctx
        out = json.dumps(box._get_activity_runs("r1"), default=str)
    assert "John Smith" not in out and "99000" not in out and "Returned 1 row(s)" in out


def test_suggestions_come_from_lineage_and_mapping_is_per_user(dq):
    from alertbot.dq.service import suggestions

    with session_scope() as s:
        user = s.query(User).filter_by(email="chinmoy@contoso.com").one()
        c = DbConnection(user_id=user.id, name="tt", kind="sqlserver", host="35.244.42.73", port=1433, database="TeamTesseract", auth="sql",
                         username="u", status="ok")
        s.add(c)
        s.flush()
        s.get(BotState, "inventory:adfp").value = {"items": [{"name": "pl_sql", "lineage": [{"step": "Copy", "writes": [
            {"kind": "table", "platform": "SQL Server", "host": "35.244.42.73", "table": "TEAMTESSERACT.ALERT_BOT.NEWS_ARTICLES2"},
            {"kind": "table", "platform": "Snowflake", "host": "psai", "table": "DB.RAW.X"},
            {"kind": "table", "platform": "SQL Server", "host": "10.0.0.9", "table": "TEAMTESSERACT.ALERT_BOT.ELSEWHERE"}]}]}]}
        s.flush()
        project = s.query(Project).filter_by(key="adfp").one()
        got = suggestions(s, user, [project])
    assert [(g["pipeline"], g["schema"], g["table"], g["match"]) for g in got] == [("pl_sql", "ALERT_BOT", "NEWS_ARTICLES2", "exact")]
    assert DqMapping.__tablename__ == "dq_mappings"


def test_checks_run_by_themselves_after_mapping_and_after_each_load(dq, spy):
    """Nobody presses anything: the cycle checks a newly mapped table, explains it, and checks it again only when its
    pipeline has loaded new data. The report then shows every check as a card and every column's empty share."""
    from alertbot.config import load_projects
    from alertbot.db import Run
    from alertbot.engine.cycle import run_cycle

    conn = _connect(dq)
    mid = _map(dq, conn)
    cfg = load_projects()["adfp"]
    first = TODAY + timedelta(hours=1)
    with session_scope() as s:  # the test runs on a fixed past clock: the mapping was made just before
        m = s.get(DqMapping, mid)
        m.created_at = m.updated_at = first - timedelta(minutes=1)
    assert run_cycle(cfg, now=first).status == "ok"
    obj = "MAIN.MAIN.NEWS_ARTICLES2"
    q = dq.api.get("/api/projects/adfp/quality").json()
    card = next(t for t in q["overview"]["tables"] if t["table"] == obj)
    assert card["checked_at"] and card["pipeline"] == "pl_sql" and q["overview"]["live"]["state"] == "idle"
    assert q["report"]["analysis"]["tables"]  # explained automatically (the model's answer, or the rules')

    with session_scope() as s:
        n_before = s.query(__import__("alertbot.db", fromlist=["MetricObservation"]).MetricObservation).filter_by(output_id=f"dq:{obj}").count()
    assert run_cycle(cfg, now=first + timedelta(minutes=10)).status == "ok"  # nothing new loaded: not checked again
    with session_scope() as s:
        from alertbot.db import MetricObservation

        assert s.query(MetricObservation).filter_by(output_id=f"dq:{obj}").count() == n_before
        project = s.query(Project).filter_by(key="adfp").one()
        s.add(Run(project_id=project.id, trigger_id="t1", site="pl_sql", pipeline="pl_sql", trigger_status="succeeded", run_id="r-new",
                  run_status="Succeeded", requested_at=first + timedelta(minutes=12), started_at=first + timedelta(minutes=12),
                  ended_at=first + timedelta(minutes=15), duration_s=180))
    assert run_cycle(cfg, now=first + timedelta(minutes=20)).status == "ok"  # pl_sql loaded: checked again
    with session_scope() as s:
        assert s.query(MetricObservation).filter_by(output_id=f"dq:{obj}").count() > n_before

    d = dq.api.get("/api/projects/adfp/quality/table", params={"name": obj}).json()
    keys = [c["key"] for c in d["cards"]]
    assert keys == ["row_count", "null_spike", "duplicates", "freshness", "schema_drift", "value_range", "invalid_type",
                    "distribution_shift", "referential_integrity", "business_rule", "run_too_slow", "run_failed",
                    "missing_partitions", "source_available"]
    by = {c["key"]: c for c in d["cards"]}
    assert by["business_rule"]["status"] == "fail" and by["referential_integrity"]["status"] == "not_set" and by["referential_integrity"]["hint"]
    assert by["source_available"]["status"] == "pass" and by["run_failed"]["value"].startswith("0 failed")
    title = next(c for c in d["columns"] if c["name"] == "TITLE")
    assert abs(title["null_rate"] - 4 / 53) < 1e-6 and next(c for c in d["columns"] if c["name"] == "AUTHOR_EMAIL")["sensitive"]
    assert d["ai"] and d["ai"]["headline"] and d["duplicates"]["rows"] == 3 and d["pipeline_runs"]
    assert SECRET_VALUE not in json.dumps(d) and SECRET_EMAIL not in json.dumps(d)


def test_a_day_without_new_rows_is_judged_not_crashed(dq):
    """SUM over zero rows is NULL in SQL: a quiet day must read as 0 empty values, not break the check."""
    db = sqlite3.connect(dq.path)
    db.execute("UPDATE NEWS_ARTICLES2 SET LOAD_DATE = '2026-01-01 00:00:00'")  # nothing loaded "today"
    db.commit()
    db.close()
    mid = _map(dq, _connect(dq))
    from alertbot.dq.service import collect_mapped
    from alertbot.engine.quality import judge_quality

    with session_scope() as s:
        ctx = _ctx(s)
        batch = collect_mapped(ctx, force=True)
        assert batch.items and batch.items[0].profile.profiled_rows == 0
        assert batch.items[0].profile.nulls["TITLE"] == 0
        outcomes = judge_quality(ctx, batch)
    assert outcomes and mid


def test_tables_the_pipelines_load_are_mapped_by_themselves(dq, monkeypatch):
    """Lineage says pl_sql writes MAIN.MAIN.NEWS_ARTICLES2 on 203.0.113.10; someone connected that database: the table is
    mapped (personal-looking columns marked sensitive) and checked with no clicks. Removing it keeps it removed."""
    from alertbot.dq import service
    from alertbot.dq.client import ConnInfo, DbClient

    with session_scope() as s:
        user = s.query(User).filter_by(email="chinmoy@contoso.com").one()
        c = DbConnection(user_id=user.id, name="tt", kind="sqlserver", host="203.0.113.10", port=1433, database="main", auth="sql",
                         username="u", status="ok")
        s.add(c)
        s.get(BotState, "inventory:adfp").value = {"items": [{"name": "pl_sql", "lineage": [{"step": "Copy", "writes": [
            {"kind": "table", "platform": "SQL Server", "host": "203.0.113.10", "table": "MAIN.MAIN.NEWS_ARTICLES2"}]}]}]}
    # the stand-in: this "SQL Server" is the test's SQLite file (nothing leaves the machine)
    from alertbot.dq import graph

    def stand_in(conn):
        return DbClient(ConnInfo(id=conn.id, kind="sqlite", host=str(dq.path), port=None, database="main", auth="sql"))

    monkeypatch.setattr(service, "client_of", stand_in)
    monkeypatch.setattr(graph, "client_of", stand_in)
    from alertbot.dq.service import collect_mapped

    with session_scope() as s:
        batch = collect_mapped(_ctx(s))
    assert batch is not None and [p.profile.table for p in batch.items] == ["MAIN.MAIN.NEWS_ARTICLES2"]
    m = dq.api.get("/api/projects/adfp/mappings").json()["mappings"]
    assert len(m) == 1 and m[0]["automatic"] and m[0]["settings"]["sensitive"] == ["AUTHOR_EMAIL"]

    assert dq.api.delete(f"/api/projects/adfp/mappings/{m[0]['id']}").status_code == 200
    with session_scope() as s:
        assert collect_mapped(_ctx(s)) is None  # removed by a person: not mapped again by itself
    assert dq.api.get("/api/projects/adfp/mappings").json()["mappings"] == []


def test_a_table_dropped_from_the_database_retires_its_mapping_until_it_is_back(dq):
    """The database is re-read too: a mapped table that's gone retires its mapping (Azure can't bring it back); once
    the table exists again, the mapping is checked again."""
    from alertbot.dq.service import reconcile_mappings, verify_tables
    from alertbot.timeutil import utcnow

    cid = _connect(dq)
    mid = _map(dq, cid)
    db = sqlite3.connect(dq.path)
    db.execute("ALTER TABLE NEWS_ARTICLES2 RENAME TO NEWS_ARTICLES2_OLD")
    db.commit()
    db.close()
    with session_scope() as s:
        out = verify_tables(s, s.get(DbConnection, cid), utcnow())
        assert out["status"] == "ok" and out["missing"] == ["main.NEWS_ARTICLES2"]
        m = s.get(DqMapping, mid)
        assert m.retired_by == "database" and "no longer exists" in m.retired_reason
        project = s.query(Project).filter_by(key="adfp").one()
        reconcile_mappings(s, project, [{"name": "pl_sql", "lineage": []}], utcnow())
        assert s.get(DqMapping, mid).retired_at is not None  # Azure's view doesn't undo the database's
    db = sqlite3.connect(dq.path)
    db.execute("ALTER TABLE NEWS_ARTICLES2_OLD RENAME TO NEWS_ARTICLES2")
    db.commit()
    db.close()
    with session_scope() as s:
        out = verify_tables(s, s.get(DbConnection, cid), utcnow())
        assert out["back"] == ["main.NEWS_ARTICLES2"] and s.get(DqMapping, mid).retired_at is None
