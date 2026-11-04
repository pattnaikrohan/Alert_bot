"""Lineage: where data comes from and goes to, across platforms (Blob → Data Factory → Snowflake → Data Factory → Azure SQL),
read only from pipeline, dataset, data flow and connection definitions, never their secrets."""

from __future__ import annotations

import json
import re

import httpx
from test_adf import FakeCredential, adf_ws  # noqa: F401 - adf_ws is a fixture

from alertbot.config import PipelineConfig
from alertbot.connectors.adf import AdfPipeline
from alertbot.db import BotState, session_scope

BASE = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.DataFactory/factories/f"
LINKED = [
    {"name": "LS_BLOB", "properties": {"type": "AzureBlobStorage", "typeProperties": {"serviceEndpoint": "https://rawdrop.blob.core.windows.net/?sv=2022&sig=SAS_SECRET"}}},
    {"name": "LS_SF", "properties": {"type": "SnowflakeV2", "typeProperties": {"accountIdentifier": "psai-csdw", "database": "DB_PROD",
                                                                               "password": {"type": "SecureString", "value": "SF_SECRET"}}}},
    {"name": "LS_SQL", "properties": {"type": "AzureSqlDatabase", "typeProperties": {"server": "reporting.database.windows.net", "database": "ReportDB",
                                                                                     "connectionString": "Server=x;Password=SQL_SECRET"}}},
]
DATASETS = {
    "DS_CSV": {"type": "DelimitedText", "linkedServiceName": {"referenceName": "LS_BLOB"},
               "typeProperties": {"location": {"type": "AzureBlobStorageLocation", "container": "inbound", "folderPath": "orders"}}},
    "DS_ORDERS": {"type": "SnowflakeV2Table", "linkedServiceName": {"referenceName": "LS_SF"}, "typeProperties": {"schema": "RAW", "table": "ORDERS"}},
    "DS_ORDERS_SQL": {"type": "AzureSqlTable", "linkedServiceName": {"referenceName": "LS_SQL"}, "typeProperties": {"schema": "dbo", "table": "Orders"}},
    "DS_DYN": {"type": "SnowflakeV2Table", "linkedServiceName": {"referenceName": "LS_SF"},
               "typeProperties": {"schema": "RAW", "table": {"value": "@dataset().t", "type": "Expression"}}},
}
PIPELINES = {
    "PL_LOAD": [
        {"name": "Copy orders", "type": "Copy", "inputs": [{"referenceName": "DS_CSV"}], "outputs": [{"referenceName": "DS_ORDERS"}]},
        {"name": "Publish", "type": "ExecutePipeline", "typeProperties": {"pipeline": {"referenceName": "PL_PUBLISH"}}},
    ],
    "PL_PUBLISH": [
        {"name": "To reporting", "type": "Copy", "inputs": [{"referenceName": "DS_ORDERS"}], "outputs": [{"referenceName": "DS_ORDERS_SQL"}]},
        {"name": "Refresh", "type": "SqlServerStoredProcedure", "linkedServiceName": {"referenceName": "LS_SQL"},
         "typeProperties": {"storedProcedureName": "dbo.refresh_marts"}},
    ],
    "PL_FLOW": [{"name": "Flow", "type": "ExecuteDataFlow", "typeProperties": {"dataflow": {"referenceName": "DF_X"}}}],
}


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix(BASE)
    if path == "/pipelines":
        return httpx.Response(200, json={"value": [{"name": n, "properties": {"activities": a}} for n, a in PIPELINES.items()]})
    if path == "/linkedservices":
        return httpx.Response(200, json={"value": LINKED})
    if m := re.fullmatch(r"/datasets/(.+)", path):
        return httpx.Response(200, json={"name": m.group(1), "properties": DATASETS[m.group(1)]})
    if path == "/dataflows/DF_X":
        return httpx.Response(200, json={"properties": {"typeProperties": {"sources": [{"dataset": {"referenceName": "DS_ORDERS"}}],
                                                                            "sinks": [{"dataset": {"referenceName": "DS_DYN"}}]}}})
    if path == "/triggers":
        return httpx.Response(200, json={"value": []})
    return httpx.Response(404, json={"error": {"message": path}})


def factory() -> AdfPipeline:
    cfg = PipelineConfig(type="adf", subscription_id="s", resource_group="rg", factory="f")
    return AdfPipeline(cfg, credential=FakeCredential(), transport=httpx.MockTransport(handler))


def test_each_step_says_what_it_reads_and_writes_on_which_platform():
    adf = factory()
    adf.list_pipelines()
    load = adf.lineage("PL_LOAD")
    copy = next(s for s in load if s["step"] == "Copy orders")
    src, dst = copy["reads"][0], copy["writes"][0]
    assert (src["platform"], src["host"], src["kind"], src["table"]) == ("Blob Storage", "rawdrop.blob.core.windows.net", "files", "inbound/orders")
    assert (dst["platform"], dst["host"], dst["table"]) == ("Snowflake", "psai-csdw", "DB_PROD.RAW.ORDERS")
    assert next(s for s in load if s["step"] == "Publish")["runs"] == "PL_PUBLISH"

    publish = {s["step"]: s for s in adf.lineage("PL_PUBLISH")}
    sql = publish["To reporting"]["writes"][0]
    assert (sql["platform"], sql["host"], sql["table"]) == ("Azure SQL", "reporting.database.windows.net", "REPORTDB.DBO.ORDERS")
    proc = publish["Refresh"]["writes"][0]
    assert proc["kind"] == "database" and proc["procedure"] == "dbo.refresh_marts" and proc["table"] == "REPORTDB"

    flow = adf.lineage("PL_FLOW")[0]
    assert flow["reads"][0]["table"] == "DB_PROD.RAW.ORDERS"
    assert flow["writes"][0]["dynamic"] is True  # the table name is a parameter: decided at run time

    everything = json.dumps(adf.inventory())
    for secret in ("SF_SECRET", "SQL_SECRET", "SAS_SECRET", "sig=", "Password"):
        assert secret not in everything


def test_lineage_graph_joins_platforms_and_shows_what_a_failure_reaches(adf_ws):  # noqa: F811
    from fastapi.testclient import TestClient

    from alertbot.api.main import app

    items = factory().inventory()
    with session_scope() as s:
        s.add(BotState(key="inventory:adfp", value={"items": items}))
    g = TestClient(app, headers={"Authorization": "Bearer dev:chinmoy@contoso.com"}).get("/api/lineage").json()
    nodes = {n["id"]: n for n in g["nodes"]}
    orders = [n for n in g["nodes"] if n["kind"] == "data" and n["object"] == "DB_PROD.RAW.ORDERS"]
    assert len(orders) == 1  # written by one pipeline, read by two others: one node
    oid = orders[0]["id"]
    edges = {(e["from"], e["to"], e["kind"]) for e in g["edges"]}
    load, publish, flow = "pipeline:adfp:PL_LOAD", "pipeline:adfp:PL_PUBLISH", "pipeline:adfp:PL_FLOW"
    assert (load, oid, "writes") in edges and (oid, publish, "reads") in edges and (oid, flow, "reads") in edges
    assert (load, publish, "runs") in edges
    sql = next(n for n in g["nodes"] if n.get("object") == "REPORTDB.DBO.ORDERS")
    assert sql["platform"] == "Azure SQL" and (publish, sql["id"], "writes") in edges
    assert set(g["platforms"]) == {"Blob Storage", "Snowflake", "Azure SQL"}
    assert nodes[load]["label"] == "PL_LOAD" and orders[0]["label"] == "ORDERS"
    # someone who can't see the project sees none of it
    other = TestClient(app, headers={"Authorization": "Bearer dev:other@contoso.com"}).get("/api/lineage").json()
    assert other["nodes"] == []
