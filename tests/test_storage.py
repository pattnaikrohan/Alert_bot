"""Warehouse cost: a table is "ready to remove" only on evidence (no use over a period the bot can see, at least the
threshold); a restart doesn't erase what the bot already saw; pipelines' tables are never suggested; costs add up; the
scripts are safe and quoted; and only the person who connected a database sees it. No real database is touched: a
stand-in client answers the catalogue queries."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from test_adf import adf_ws  # noqa: F401 - fixture

from alertbot.db import BotState, DbConnection, Project, User, session_scope
from alertbot.dq import storage as S
from alertbot.timeutil import iso

NOW = datetime(2026, 10, 22, 12, 0)
GB = 1024 ** 3


def _read(tables, start, usage="dmv", deps=None):
    return {"tables": tables, "usage": usage, "usage_error": None, "server_start": start, "deps": deps or {}}


def _t(schema, table, gb, *, created, last_read=None, last_write=None):
    return {"schema": schema, "table": table, "rows": 1000.0, "bytes": gb * GB, "created": iso(created), "ddl": iso(created),
            "last_read": iso(last_read) if last_read else None, "last_write": iso(last_write) if last_write else None}


def test_unused_needs_evidence_over_the_whole_threshold():
    old = NOW - timedelta(days=900)
    tables = {
        "dbo.OLD": _t("dbo", "OLD", 10, created=old),  # never touched since the server started 300 days ago
        "dbo.HOT": _t("dbo", "HOT", 5, created=old, last_read=NOW - timedelta(days=2)),
        "dbo.LOADED": _t("dbo", "LOADED", 2, created=old, last_write=NOW - timedelta(hours=3)),
        "dbo.NEW": _t("dbo", "NEW", 1, created=NOW - timedelta(days=40)),
    }
    st = S.merge(None, _read(tables, iso(NOW - timedelta(days=300))), NOW)
    c = {k: S.classify(t, st, "sqlserver", NOW, 180, [], None) for k, t in st["tables"].items()}
    assert c["dbo.OLD"]["status"] == "unused" and c["dbo.OLD"]["confidence"] == "high"
    assert c["dbo.HOT"]["status"] == "in_use"
    assert c["dbo.LOADED"]["status"] == "write_only"  # loaded every day, but nobody has read it in 300 days
    assert c["dbo.NEW"]["status"] == "watching" and 139 <= c["dbo.NEW"]["sure_in_days"] <= 141  # only 40 days of evidence
    assert S.classify(st["tables"]["dbo.OLD"], st, "sqlserver", NOW, 180, ["pipeline pl_sql"], None)["status"] == "in_use"
    assert S.classify(st["tables"]["dbo.OLD"], st, "sqlserver", NOW, 180, [], {"by": "Jnana"})["status"] == "kept"
    # a server that only started 20 days ago can't prove anything yet
    young = S.merge(None, _read(tables, iso(NOW - timedelta(days=20))), NOW)
    assert S.classify(young["tables"]["dbo.OLD"], young, "sqlserver", NOW, 180, [], None)["status"] == "watching"


def test_a_restart_doesnt_erase_what_the_bot_saw():
    old = NOW - timedelta(days=900)
    first = S.merge(None, _read({"dbo.T": _t("dbo", "T", 1, created=old, last_read=NOW - timedelta(days=10))}, iso(NOW - timedelta(days=300))), NOW)
    # an hour later the server restarted (counters emptied): the read the bot recorded stays, and so does its watch
    later = NOW + timedelta(hours=2)
    second = S.merge(first, _read({"dbo.T": _t("dbo", "T", 1, created=old)}, iso(NOW + timedelta(hours=1))), later)
    assert second["tables"]["dbo.T"]["last_read"] == first["tables"]["dbo.T"]["last_read"]
    assert second["covered_since"] == first["covered_since"]  # restarted within the gap the bot covers: still continuous
    # but a restart after a long unwatched spell: only trust from the restart
    much_later = NOW + timedelta(days=5)
    third = S.merge(second, _read({"dbo.T": _t("dbo", "T", 1, created=old)}, iso(NOW + timedelta(days=4))), much_later)
    assert third["covered_since"] == iso(NOW + timedelta(days=4))


def test_snowflake_without_access_history_is_only_a_maybe():
    old = NOW - timedelta(days=900)
    t = {**_t("RAW", "STALE", 3, created=old), "altered": iso(NOW - timedelta(days=400))}
    st = S.merge(None, {"tables": {"RAW.STALE": t}, "usage": "last_altered", "usage_error": "no access", "server_start": None, "deps": {}}, NOW)
    c = S.classify(st["tables"]["RAW.STALE"], st, "snowflake", NOW, 180, [], None)
    assert c["status"] == "maybe" and c["confidence"] == "low" and "reads can't be seen" in c["why"] and 399 <= c["idle_days"] <= 401
    sf = S.script(DbConnection(name="SF", kind="snowflake", database="DB"), st, ["RAW.STALE"], "archive", "Jnana", NOW)
    assert any("check nobody still queries them" in w for w in sf["warnings"]) and "UNDROP" not in sf["sql"]
    assert all(len(line) <= 110 for line in sf["sql"].splitlines() if line.startswith("--"))


def test_scripts_are_safe_quoted_and_leave_out_what_shouldnt_go():
    conn = DbConnection(name="TT", kind="sqlserver", host="203.0.113.5", database="TeamTesseract")
    old = NOW - timedelta(days=900)
    st = S.merge(None, _read({"dbo.OLD": _t("dbo", "OLD", 2, created=old), "dbo.we]ird": _t("dbo", "we]ird", 1, created=old),
                              "dbo.KEPT": _t("dbo", "KEPT", 1, created=old)}, iso(old),
                             deps={"dbo.OLD": [{"object": "dbo.v_old", "kind": "view or procedure"}]}), NOW)
    st["keep"] = {"dbo.KEPT": {"by": "Jnana"}}
    s = S.script(conn, st, ["dbo.OLD", "dbo.we]ird", "dbo.KEPT", "dbo.GONE"], "drop", "Jnana", NOW, price=0.115)
    sql = s["sql"]
    assert "BEGIN TRANSACTION;" in sql and "COMMIT TRANSACTION;" in sql and "IF OBJECT_ID(N'dbo.OLD', N'U') IS NOT NULL DROP TABLE [dbo].[OLD];" in sql
    assert "we]ird" not in sql.split("-- Left out:")[1].split("\n\n")[1] and "DROP TABLE [dbo].[KEPT]" not in sql and s["tables"] == 1
    assert any("v_old" in w for w in s["warnings"]) and any("KEPT" in x for x in s["skipped"]) and any("GONE" in x for x in s["skipped"])
    assert any("we]ird" in x and "by hand" in x for x in s["skipped"])
    assert s["monthly"] == round(2 * 0.115, 2) and "never runs this" in sql
    assert S._ident("sqlserver", "Order Lines") == "[Order Lines]" and S._ident("snowflake", "Order Lines") == '"Order Lines"'
    a = S.script(conn, st, ["dbo.OLD"], "archive", "Jnana", NOW)["sql"]
    assert "ALTER SCHEMA [archive] TRANSFER [dbo].[OLD]" in a and "CREATE SCHEMA [archive]" in a
    snow = S.script(DbConnection(name="SF", kind="snowflake", host="acct", database="DB_PROD"), st, ["dbo.OLD"], "drop", "Jnana", NOW)["sql"]
    assert 'DROP TABLE IF EXISTS "DB_PROD"."dbo"."OLD";' in snow and 'UNDROP TABLE "DB_PROD"."dbo"."OLD";' in snow


class FakeSqlServer:
    """Answers the catalogue queries like SQL Server would (local times, 5 h 30 ahead of UTC)."""

    def __init__(self, now_local):
        self.now = now_local

    def meta(self, mq, limit=5000):
        old = self.now - timedelta(days=700)
        return {
            "storage_clock": [(330,)],
            "storage_start": [(self.now - timedelta(days=250),)],
            "storage_sizes": [("dbo", "ARCHIVE_2023", old, old, 1_000_000, 12 * GB), ("Alert_bot", "NEWS_ARTICLES", old, old, 500, 0.01 * GB),
                              ("dbo", "STAGING_OLD", old, old, 10, 0.5 * GB)],
            "storage_usage": [("Alert_bot", "NEWS_ARTICLES", self.now - timedelta(hours=1), self.now - timedelta(hours=1))],
            "storage_deps": [],
        }.get(mq.name, [])


def test_scan_report_keep_price_and_script_through_the_api(adf_ws, monkeypatch):  # noqa: F811
    from alertbot.api.main import app
    from alertbot.dq import service

    monkeypatch.setattr(service, "client_of", lambda conn: FakeSqlServer(datetime.now() + timedelta(hours=5, minutes=30) - timedelta(hours=5, minutes=30)))
    with session_scope() as s:
        user = s.query(User).filter_by(email="chinmoy@contoso.com").one()
        c = DbConnection(user_id=user.id, name="TeamTesseract", kind="sqlserver", host="203.0.113.9", port=1433, database="TeamTesseract",
                         auth="sql", username="reader", status="ok")
        s.add(c)
        s.flush()
        cid = c.id
        p = s.query(Project).filter_by(key="adfp").one()
        s.add(BotState(key="inventory:adfp", value={"items": [{"name": "pl_old", "type": "pipeline", "lineage": [{"step": "c", "type": "Copy", "reads": [],
                       "writes": [{"kind": "table", "platform": "SQL Server", "host": "203.0.113.9", "table": "TEAMTESSERACT.DBO.STAGING_OLD"}]}]}]}))
        assert p is not None
    api = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com", "X-AlertBot": "1"})
    r = api.post("/api/storage/scan?days=180").json()
    db = next(d for d in r["databases"] if d["id"] == cid)
    by = {t["key"]: t for t in db["tables"]}
    assert by["dbo.ARCHIVE_2023"]["status"] == "unused" and by["dbo.ARCHIVE_2023"]["gb"] == 12.0
    assert by["Alert_bot.NEWS_ARTICLES"]["status"] == "in_use"
    assert by["dbo.STAGING_OLD"]["status"] == "in_use" and "pipeline pl_old" in by["dbo.STAGING_OLD"]["why"]  # loaded by a pipeline: never suggested
    assert r["summary"]["unused_tables"] == 1 and r["summary"]["monthly"] == round(12 * 0.115, 2)
    assert db["usage"] == "dmv" and db["grant"] is None
    api.put(f"/api/storage/{cid}/price", json={"price": 0.25})
    assert api.get("/api/storage?days=180").json()["summary"]["monthly"] == 3.0
    one = api.get(f"/api/storage?days=180&conn={cid}").json()  # one database chosen: only it, but every database is offered
    assert [d["id"] for d in one["databases"]] == [cid] and one["scope"] == cid and cid in [d["id"] for d in one["all_databases"]]
    assert one["summary"]["monthly"] == 3.0 and one["summary"]["all_monthly"] == 3.0  # pipelines aren't in one database
    other = api.get(f"/api/storage?days=180&conn={cid + 999}").json()
    assert other["databases"] == [] and other["summary"]["unused_tables"] == 0
    sc = api.post(f"/api/storage/{cid}/script", json={"keys": ["dbo.ARCHIVE_2023", "dbo.STAGING_OLD"], "mode": "drop"}).json()
    assert sc["tables"] == 1 and "DROP TABLE [dbo].[ARCHIVE_2023]" in sc["sql"] and any("pl_old" in x for x in sc["skipped"])
    api.put(f"/api/storage/{cid}/keep", json={"key": "dbo.ARCHIVE_2023", "reason": "audit"})
    assert api.get("/api/storage").json()["summary"]["unused_tables"] == 0
    other = TestClient(app, headers={"Authorization": "Bearer dev:other@contoso.com", "X-AlertBot": "1"})
    assert other.post(f"/api/storage/{cid}/script", json={"keys": ["dbo.ARCHIVE_2023"]}).status_code == 404
    assert all(d["id"] != cid for d in other.get("/api/storage").json()["databases"])
