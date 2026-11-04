"""Snowflake compute cost: spend and forecast, idle waste and sizing advice with safe scripts, alerts (spikes, runaway
queries, budget), pipelines' share, the assistant and the morning report. A stand-in Snowflake answers each query the way
ACCOUNT_USAGE would; nothing connects anywhere, and no query ever asks for a query's text."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_adf import adf_ws  # noqa: F401 - fixture

from alertbot.db import BotState, DbConnection, Project, Run, User, session_scope
from alertbot.dq import compute as C
from alertbot.dq import sql as T
from alertbot.timeutil import utcnow


class FakeSnowflake:
    """Two warehouses for 40 days: ETL_WH (Large, waits 10 minutes idle, small quick queries, a spike in the last hour)
    and BI_WH (Small, busy, queries queue). A pipeline login ADF_SVC loads every day at 02:00."""

    def __init__(self, now: datetime, rate: float | None = None):
        self.now, self.rate, self.asked = now, rate, []

    def warehouses(self):
        return (["name", "state", "type", "size", "min_cluster_count", "max_cluster_count", "auto_suspend", "auto_resume", "resource_monitor"],
                [("ETL_WH", "SUSPENDED", "STANDARD", "Large", 1, 1, 600, "true", "null"),
                 ("BI_WH", "STARTED", "STANDARD", "Small", 1, 1, 60, "true", "BI_MONITOR")])

    def meta(self, mq, limit=5000):
        self.asked.append(mq.sql)
        today = self.now.date()
        days = [today - timedelta(days=i) for i in range(1, 41)]
        last_hour = self.now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        if mq.name == "compute_daily":
            return [(d, "WAREHOUSE_METERING", 30.0, 30.0) for d in days] + [(d, "SERVERLESS_TASK", 2.0, 2.0) for d in days] + \
                   [(today, "WAREHOUSE_METERING", 10.0, 10.0)]
        if mq.name == "compute_hourly":
            rows = []
            for h in range(1, 15 * 24):
                t = last_hour - timedelta(hours=h - 1)
                rows.append(("ETL_WH", t.replace(tzinfo=UTC), 12.0 if h == 1 else 1.0, 12.0 if h == 1 else 1.0))
            return rows
        if mq.name == "compute_wh_daily":
            return [("ETL_WH", d, 24.0, 24.0) for d in days] + [("BI_WH", d, 6.0, 6.0) for d in days]
        if mq.name == "compute_attributed":
            return [("ETL_WH", d, 4.0, 100) for d in days[:30]] + [("BI_WH", d, 5.5, 800) for d in days[:30]]
        if mq.name == "compute_users":
            return [("ADF_SVC", d, 4.0, 100) for d in days[:30]] + [("JANE", d, 5.5, 800) for d in days[:30]] + \
                   [("JANE", days[0], 20.0, 50)]
        if mq.name == "compute_tags":
            return [("alertbot-data-checks", 0.6, 300), ("", 280.0, 27000)]
        if mq.name == "compute_roles":
            return [("ANALYST", 165.0, 24000), ("LOADER", 120.0, 3000)]
        if mq.name == "compute_shapes":
            return [("abc123", 60.0, 900, self.now, "01b2c3d4-0000-1111-2222-333344445555", "BI_WH", "JANE", "SELECT", "DB_PROD", "RAW", 4200.0, 1.2e9, "")]
        if mq.name == "compute_wh_stats":
            return [("ETL_WH", "Large", 3000, 900.0, 0, 0.0, 0.0, 0.0, 1e6), ("BI_WH", "Small", 24000, 30000.0, 6000, 8000.0, 1e9, 2e9, 2e9)]
        if mq.name == "compute_storage":
            return [(d, 2.0 * 1024 ** 4) for d in days] + [(today, 2.0 * 1024 ** 4)]
        if mq.name == "compute_rate":
            return [(self.rate, "USD")] if self.rate else [(None, None)]
        if mq.name == "compute_running":
            return [("01b2-run-away", "JANE", "BI_WH", "Small", self.now - timedelta(minutes=95), 95 * 60)]
        if mq.name == "compute_pipeline_buckets":
            assert mq.params == {"u0": "ADF_SVC"}
            out = []
            for d in days[:30]:
                at = datetime.combine(d, datetime.min.time()) + timedelta(hours=2, minutes=5)
                out.append((at.replace(tzinfo=UTC), "ADF_SVC", "", 3.0, 80))
                out.append(((at + timedelta(hours=10)).replace(tzinfo=UTC), "ADF_SVC", "", 1.0, 20))  # no run then: unlinked
            return out
        raise AssertionError(mq.name)


INVENTORY = {"items": [{"name": "pl_load_ais", "type": "pipeline", "steps": 1, "triggers": [], "connections": [],
                        "lineage": [{"step": "Copy", "type": "Copy", "reads": [], "writes": [{"kind": "table", "platform": "Snowflake", "table": "DB_PROD.RAW.AIS"}]}]}]}


@pytest.fixture
def cx(adf_ws, monkeypatch):  # noqa: F811
    from alertbot.dq import service

    now = utcnow()
    fake = FakeSnowflake(now)
    monkeypatch.setattr(service, "client_of", lambda conn: fake)
    with session_scope() as s:
        user = s.query(User).filter_by(email="chinmoy@contoso.com").one()
        c = DbConnection(user_id=user.id, name="Risk Monitor", kind="snowflake", host="xy12345.west-europe.azure", database="DB_PROD",
                         auth="password", username="ALERTBOT_RO", status="ok", options={"warehouse": "ALERTBOT_WH"})
        s.add(c)
        s.flush()
        cid = c.id
        p = s.query(Project).filter_by(key="adfp").one()
        s.add(BotState(key="inventory:adfp", value=INVENTORY))
        for i in range(1, 31):
            st = datetime.combine(now.date() - timedelta(days=i), datetime.min.time()) + timedelta(hours=2)
            s.add(Run(project_id=p.id, trigger_id=f"t{i}", site="pl_load_ais", trigger_status="succeeded", run_id=f"r{i}", pipeline="Schedule · daily",
                      run_status="Succeeded", requested_at=st, started_at=st, ended_at=st + timedelta(minutes=20), duration_s=1200))
    api = TestClient(__import__("alertbot.api.main", fromlist=["app"]).app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com", "X-AlertBot": "1"})
    yield type("Cx", (), {"api": api, "cid": cid, "fake": fake, "now": now})


def test_spend_waste_and_who_from_a_scan(cx):
    r = cx.api.post("/api/compute/scan").json()
    a = r["accounts"][0]
    assert a["price"] == C.LIST_PRICE and a["price_from"] == "list" and a["error"] is None
    sm = a["summary"]
    assert sm["yesterday"] == 32 * 3.0 and sm["usual_day"] == 96.0 and sm["yesterday_change"] == 0.0
    assert sm["own_cost"] == round(0.6 * 3.0, 2) and sm["own_queries"] == 300  # AlertBot's own checks, priced openly
    assert sm["storage_tb"] == 2.0 and sm["storage_month"] == 2.0 * C.STORAGE_TB
    wh = {w["name"]: w for w in a["warehouses"]}
    assert wh["ETL_WH"]["idle_credits"] == round(30 * 24 - 30 * 4, 3) and wh["ETL_WH"]["credits_per_hour"] == 8 and wh["ETL_WH"]["auto_suspend"] == 600
    assert wh["BI_WH"]["resource_monitor"] == "BI_MONITOR"
    assert a["users"][0]["name"] == "JANE" and any(u["name"] == "ADF_SVC" and u["service"] for u in a["users"])
    assert a["shapes"][0]["link"].startswith("https://app.snowflake.com/west-europe.azure/xy12345/#/compute/history/queries/01b2c3d4")
    assert {s["name"] for s in a["services"]} == {"Warehouses", "Serverless tasks"}
    # never a query's text
    assert all("QUERY_TEXT" not in sql.upper() for sql in cx.fake.asked)
    # the price: the contract's rate when Snowflake tells it, the person's own above all
    cx.fake.rate = 2.4
    assert cx.api.post("/api/compute/scan").json()["accounts"][0]["price"] == 2.4
    v = cx.api.put(f"/api/compute/{cx.cid}/settings", json={"price": 2.1}).json()
    assert v["price"] == 2.1 and v["price_from"] == "yours"


def test_savings_are_explained_and_the_script_is_safe(cx):
    a = cx.api.post("/api/compute/scan").json()["accounts"][0]
    recs = {r["id"]: r for r in a["recommendations"]}
    sus = recs["suspend:ETL_WH"]
    assert sus["monthly"] == round((30 * 24 - 30 * 4) * 0.9 * 3.0, 2) and "10 minutes" in sus["why"]
    assert "downsize:ETL_WH" in recs and recs["downsize:ETL_WH"]["to"] == "Medium"
    assert "upsize:BI_WH" in recs and "queue" in recs["upsize:BI_WH"]["why"]
    assert "monitor:ETL_WH" in recs and "monitor:BI_WH" not in recs  # BI_WH has one already
    assert a["summary"]["could_save"] == round(sum(r["monthly"] for r in a["recommendations"]), 2)
    sc = cx.api.post(f"/api/compute/{cx.cid}/script", json={"ids": ["suspend:ETL_WH", "downsize:ETL_WH", "monitor:ETL_WH", "nope:X"]}).json()
    sql = sc["sql"]
    assert 'ALTER WAREHOUSE "ETL_WH" SET AUTO_SUSPEND = 60;' in sql and '-- undo: ALTER WAREHOUSE "ETL_WH" SET AUTO_SUSPEND = 600;' in sql
    assert "ALTER WAREHOUSE \"ETL_WH\" SET WAREHOUSE_SIZE = 'MEDIUM';" in sql and "never runs this" in sql
    assert 'CREATE RESOURCE MONITOR IF NOT EXISTS "ALERTBOT_ETL_WH_MONTHLY" WITH CREDIT_QUOTA = 864' in sql
    assert sc["items"] == 3 and any("nope:X" in x for x in sc["skipped"])
    other = TestClient(cx.api.app, headers={"Authorization": "Bearer dev:other@contoso.com", "X-AlertBot": "1"})
    assert other.post(f"/api/compute/{cx.cid}/script", json={"ids": ["suspend:ETL_WH"]}).status_code == 404
    assert other.get("/api/compute").json()["accounts"] == []


def test_alerts_spike_runaway_and_budget(cx, monkeypatch):
    cx.api.put(f"/api/compute/{cx.cid}/settings", json={"budget": 2000, "runaway_minutes": 60})
    a = cx.api.post("/api/compute/scan").json()["accounts"][0]
    kinds = {x["kind"]: x for x in a["alerts"]}
    assert "12×" in kinds["spike"]["title"] and kinds["spike"]["wh"] == "ETL_WH"
    assert "95 minutes" in kinds["runaway"]["title"] and a["running"][0]["long"] is True
    mtd = a["summary"]["mtd"]
    assert ("budget" in kinds) == (mtd >= 1600 or a["summary"]["forecast"] > 2100)
    assert all(x["delivery"] == "no_flow" for x in a["alerts"])  # no Teams flow in tests: kept, marked, not lost
    again = cx.api.post("/api/compute/scan").json()["accounts"][0]
    assert len(again["alerts"]) == len(a["alerts"])  # the same alert isn't raised twice


def test_pipelines_get_their_share_and_the_assistant_and_report_know(cx):
    a = cx.api.post("/api/compute/scan").json()["accounts"][0]
    p = a["pipelines"]
    row = next(r for r in p["rows"] if r["pipeline"] == "pl_load_ais")
    # every day's 02:05 bucket lands in that day's run (the run from exactly 30 days ago may fall outside the window)
    assert row["runs"] in (29, 30) and row["per_run"] == 3.0 and row["credits"] == 3.0 * row["runs"] and row["how"] == "time"
    assert p["unlinked"] == 30.0 + (90.0 - row["credits"]) and p["users"] == ["ADF_SVC"] and p["users_set"] is False
    rep = cx.api.get("/api/pipeline-report?days=7").json()
    assert next(x for x in rep["pipelines"] if x["name"] == "pl_load_ais")["snowflake"]["per_run"] == 9.0
    from alertbot.ai.ask import answer

    with session_scope() as s:
        u = s.query(User).filter_by(email="chinmoy@contoso.com").one()
        ans = answer(s, u, "why did snowflake spend jump yesterday?")
        assert ans.topic == "compute" and "Risk Monitor: yesterday $96.00" in ans.text and "JANE +$" in ans.text
        from alertbot.dq.compute import morning_line

        line = morning_line(s, s.query(Project).filter_by(key="adfp").one(), utcnow())
        assert line.startswith("Snowflake compute Risk Monitor: yesterday $96") and "ETL_WH" in line


def test_only_show_warehouses_skips_the_select_guard():
    from alertbot.dq.client import READ_ONLY_STATEMENTS

    assert frozenset({"SHOW WAREHOUSES"}) == READ_ONLY_STATEMENTS
    for name, q in C.queries().items():
        T.assert_read_only(q.sql)
        assert "QUERY_TEXT" not in q.sql.upper(), name
    with pytest.raises(T.UnsafeStatement):
        T.assert_read_only("SHOW TABLES")
    assert C._size_step("X-Large", -1) == "Large" and C._size_step("X-Small", -1) is None and C.size_index("XSMALL") == 0
    assert C.snowsight("myorg-myacct", "01b2c3d4-0000-1111-2222-333344445555").startswith("https://app.snowflake.com/myorg/myacct/")
    assert C.snowsight("acct", "x'; drop") is None


def test_reporting_tools_arent_guessed_as_pipeline_logins():
    users = [{"user": u} for u in ("ADF_SVC", "TABLEAU_SVC", "POWERBI_SERVICE", "DBT_PROD", "JANE")]
    assert C.guess_pipeline_users(users) == ["ADF_SVC", "DBT_PROD"]
