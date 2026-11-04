"""Azure Data Factory pipeline adapters.

`AdfPipeline` talks to the ADF REST API read-only (managed identity / service principal, or a cached
device-code sign-in for a pilot): run history, activity runs, schedule triggers and pipeline definitions.
`FakeAdfPipeline` reads simulated run history from the prototype estate and
returns exactly the same shapes, so everything above it is real code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import sqlalchemy as sa

from ..config import PipelineConfig, SourceConfig
from ..timeutil import iso, to_naive_utc, utcnow
from .base import ActivityRun, PipelineRun
from .sql_source import engine_for

API_VERSION = "2018-06-01"
ARM = "https://management.azure.com"


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return to_naive_utc(value)
    text = str(value).replace("Z", "+00:00")
    # ADF returns 7 fractional digits; Python accepts at most 6.
    if "." in text:
        head, _, tail = text.partition(".")
        frac = "".join(ch for ch in tail if ch.isdigit())
        tz = tail[len(frac):]
        text = f"{head}.{frac[:6]}{tz}"
    return to_naive_utc(datetime.fromisoformat(text))


def _seconds(ms: Any) -> float | None:
    return None if ms in (None, "") else float(ms) / 1000.0


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


class AdfAccessError(RuntimeError):
    """Azure answered, but the signed-in identity can't read this factory (or it doesn't exist)."""


@dataclass(frozen=True)
class Schedule:
    trigger: str
    every: timedelta  # how often the trigger fires (approximate for complex schedules)
    kind: str  # ScheduleTrigger | TumblingWindowTrigger


_UNIT = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1),
         "week": timedelta(days=7), "month": timedelta(days=30)}


def _trigger_interval(props: dict[str, Any]) -> timedelta | None:
    tp = props.get("typeProperties") or {}
    rec = tp.get("recurrence") or tp  # TumblingWindow keeps frequency/interval at the top
    unit = _UNIT.get(str(rec.get("frequency", "")).lower())
    if unit is None:
        return None
    every = unit * int(rec.get("interval") or 1)
    sched = rec.get("schedule") or {}
    # e.g. daily at hours [4, 10, 16, 22] → every 6 h; weekly on 2 weekdays → every 3.5 days
    fires = max(1, len(sched.get("hours") or [None])) * max(1, len(sched.get("minutes") or [None]))
    if sched.get("weekDays") and unit >= timedelta(days=7):
        fires *= len(sched["weekDays"])
    return every / fires


class AdfPipeline:  # network calls need Azure; tests drive it with an httpx MockTransport
    """Read-only Azure Data Factory REST client: runs, activity runs, triggers and pipeline definitions."""

    scope = f"{ARM}/.default"
    api_version = API_VERSION
    service = "Data Factory"

    def __init__(self, cfg: PipelineConfig, *, credential: Any = None, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self._credential = credential
        self.factory_id = (
            f"/subscriptions/{cfg.subscription_id}/resourceGroups/{cfg.resource_group}"
            f"/providers/Microsoft.DataFactory/factories/{cfg.factory}"
        )
        self.name = cfg.factory
        self._base = f"{ARM}{self.factory_id}"
        self._client = httpx.Client(timeout=45, transport=transport)
        self._definitions: dict[str, list[dict[str, Any]]] = {}
        self._datasets: dict[str, dict[str, Any]] = {}
        self._linked: dict[str, dict[str, Any]] | None = None
        self._meta: dict[str, dict[str, Any]] = {}
        self._raw: dict[str, list[dict[str, Any]]] = {}

    def forget(self) -> None:
        """Drop what was read before (definitions, datasets, linked services): the next read is Azure as it is now."""
        self._definitions, self._datasets, self._linked, self._meta, self._raw = {}, {}, None, {}, {}

    # ── plumbing ──
    def _headers(self) -> dict[str, str]:
        from .azure_auth import SignInRequired, credential_for

        if self._credential is None:
            self._credential = credential_for(self.cfg)
        try:
            token = self._credential.get_token(self.scope).token
        except SignInRequired:
            raise
        except Exception as exc:  # AuthenticationRequiredError, expired refresh token, CA policy, …
            self._credential = None
            raise SignInRequired(f"The bot's Azure sign-in needs renewing ({type(exc).__name__}). Run: alertbot adf-login") from exc
        return {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, url: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        query = {} if "api-version=" in url else {"api-version": self.api_version}
        r = self._client.request(method, url, params={**query, **(params or {})}, headers=self._headers(), **kw)
        if r.status_code in (401, 403):
            raise AdfAccessError(f"No access to {self.service} {self.name} ({r.status_code}): {_error_text(r)}")
        if r.status_code == 404 and "pipelineruns/" not in url and "/runs/" not in url:
            raise AdfAccessError(f"{self.service} {self.name}: not found: {url.removeprefix(self._base) or self.name}")
        r.raise_for_status()
        return r.json() if r.content else {}

    def monitor_url(self, run_id: str) -> str:
        """Deep link to the run in ADF Studio's monitor."""
        return f"https://adf.azure.com/en/monitoring/pipelineruns/{run_id}?factory={self.factory_id}"

    # ── factory contents ──
    def list_pipelines(self) -> list[str]:
        names: list[str] = []
        url: str | None = f"{self._base}/pipelines"
        while url:
            d = self._call("GET", url)
            for p in d.get("value", []):
                names.append(p["name"])
                props = p.get("properties") or {}
                self._meta[p["name"]] = {"folder": (props.get("folder") or {}).get("name"), "description": props.get("description") or "",
                                         "steps": len(props.get("activities") or [])}
                if props.get("activities"):
                    self._raw[p["name"]] = props["activities"]
                    self._definitions.setdefault(p["name"], [_static_node(a) for a in props["activities"]])
            url = d.get("nextLink")
        return sorted(names)

    def triggers(self) -> dict[str, list[dict[str, Any]]]:
        """Every trigger (schedule, tumbling window, storage event, custom event), per pipeline it starts."""
        out: dict[str, list[dict[str, Any]]] = {}
        url: str | None = f"{self._base}/triggers"
        while url:
            d = self._call("GET", url)
            for t in d.get("value", []):
                props = t.get("properties") or {}
                kind = props.get("type", "")
                every = _trigger_interval(props) if kind in ("ScheduleTrigger", "TumblingWindowTrigger") else None
                refs = [props.get("pipeline")] if kind == "TumblingWindowTrigger" else props.get("pipelines") or []
                for ref in refs:
                    name = ((ref or {}).get("pipelineReference") or {}).get("referenceName")
                    if name:
                        out.setdefault(name, []).append({"name": t.get("name", ""), "kind": _TRIGGER_KIND.get(kind, kind),
                                                         "started": props.get("runtimeState") == "Started",
                                                         "every_hours": round(every.total_seconds() / 3600, 3) if every else None})
            url = d.get("nextLink")
        return out

    def _dataflow(self, name: str) -> dict[str, list[str]]:
        """A mapping data flow's source and sink datasets."""
        d = self._call("GET", f"{self._base}/dataflows/{name}")
        tp = (d.get("properties") or {}).get("typeProperties") or {}
        ref = lambda xs: [((x.get("dataset") or {}).get("referenceName")) for x in xs or [] if (x.get("dataset") or {}).get("referenceName")]  # noqa: E731
        return {"sources": ref(tp.get("sources")), "sinks": ref(tp.get("sinks"))}

    def lineage(self, pipeline: str) -> list[dict[str, Any]]:
        """Per step: the data it reads and writes (on any platform), and the pipelines it runs. From the definition only."""
        if pipeline not in self._raw:
            self._definition(pipeline)
        out = []
        for a in _walk(self._raw.get(pipeline) or []):
            kind, tp, step = a.get("type", ""), a.get("typeProperties") or {}, a.get("name", "")
            reads = [d.get("referenceName") for d in a.get("inputs") or [] if d.get("referenceName")]
            writes = [d.get("referenceName") for d in a.get("outputs") or [] if d.get("referenceName")]
            runs = None
            ds = (tp.get("dataset") or {}).get("referenceName")
            if kind in ("Lookup", "GetMetadata", "Validation") and ds:
                reads.append(ds)
            elif kind == "Delete" and ds:
                writes.append(ds)
            elif kind == "ExecuteDataFlow":
                try:
                    flow = self._dataflow((tp.get("dataflow") or {}).get("referenceName", ""))
                    reads += flow["sources"]
                    writes += flow["sinks"]
                except Exception:  # noqa: BLE001 - a flow we can't read just shows no data
                    pass
            elif kind == "ExecutePipeline":
                runs = (tp.get("pipeline") or {}).get("referenceName")
            node = lambda n: self.dataset_target(n)  # noqa: E731
            r = [node(x) for x in dict.fromkeys(reads)]
            w = [node(x) for x in dict.fromkeys(writes)]
            if kind in ("SqlServerStoredProcedure", "Script"):  # runs SQL in a database: shown against that database
                ls = (a.get("linkedServiceName") or {}).get("referenceName", "")
                conn = self._linked_services().get(ls, {}) if ls else {}
                w.append({"kind": "database", "table": (conn.get("database") or ls).upper(), "database": conn.get("database"), "dataset": None,
                          "linked_service": ls, "env": _env_of(f"{conn.get('database') or ''} {ls}"), "dynamic": False,
                          "platform": conn.get("platform") or "Database", "host": conn.get("host"),
                          "procedure": _plain(tp.get("storedProcedureName")) if kind == "SqlServerStoredProcedure" else None})
            if r or w or runs:
                out.append({"step": step, "type": kind, "reads": r, "writes": w, "runs": runs})
        return out

    def inventory(self) -> list[dict[str, Any]]:
        """Every pipeline in the factory / workspace, run or not: what the app lists as "available"."""
        names = self.list_pipelines()
        try:
            trig = self.triggers()
        except Exception:  # noqa: BLE001 - triggers are a bonus
            trig = {}
        skip = set(self.cfg.exclude)
        watched = set(self.cfg.pipelines) if self.cfg.pipelines else None
        try:
            linked = self._linked_services()
        except Exception:  # noqa: BLE001 - connections are a bonus
            linked = {}
        def lineage_of(n: str) -> list[dict[str, Any]] | None:
            try:
                return self.lineage(n)
            except Exception:  # noqa: BLE001 - lineage is a bonus; None = unknown (not "loads nothing")
                return None

        return [{"name": n, "type": "pipeline", "state": "Enabled", **self._meta.get(n, {}), "triggers": trig.get(n, []),
                 "connections": connections(self._raw.get(n, []), linked), "lineage": lineage_of(n),
                 "watched": n not in skip and (watched is None or n in watched)} for n in names]

    def watched_pipelines(self) -> list[str]:
        names = self.cfg.pipelines or self.list_pipelines()
        return [n for n in names if n not in set(self.cfg.exclude)]

    def schedules(self) -> dict[str, list[Schedule]]:
        """Started schedule / tumbling-window triggers per pipeline (manual or event-only pipelines have none)."""
        out: dict[str, list[Schedule]] = {}
        url: str | None = f"{self._base}/triggers"
        while url:
            d = self._call("GET", url)
            for t in d.get("value", []):
                props = t.get("properties") or {}
                kind = props.get("type", "")
                if kind not in ("ScheduleTrigger", "TumblingWindowTrigger") or props.get("runtimeState") != "Started":
                    continue
                every = _trigger_interval(props)
                if every is None:
                    continue
                refs = [props.get("pipeline")] if kind == "TumblingWindowTrigger" else props.get("pipelines") or []
                for ref in refs:
                    name = ((ref or {}).get("pipelineReference") or {}).get("referenceName")
                    if name:
                        out.setdefault(name, []).append(Schedule(t.get("name", ""), every, kind))
            url = d.get("nextLink")
        return out

    # ── runs ──
    @staticmethod
    def _run(d: dict[str, Any]) -> PipelineRun:
        invoked = d.get("invokedBy") or {}
        return PipelineRun(
            run_id=d["runId"],
            pipeline=d.get("pipelineName", ""),
            status=d.get("status", ""),
            started_at=_dt(d.get("runStart")),
            ended_at=_dt(d.get("runEnd")),
            duration_s=_seconds(d.get("durationInMs")),
            parameters=d.get("parameters") or {},
            message=d.get("message") or "",
            invoked_by=" · ".join(x for x in (invoked.get("invokedByType"), invoked.get("name")) if x),
        )

    def query_runs(self, since: datetime, until: datetime, pipelines: list[str] | None = None) -> list[PipelineRun]:
        """Every run updated in [since, until), optionally limited to some pipelines (paged)."""
        body: dict[str, Any] = {"lastUpdatedAfter": since.isoformat() + "Z", "lastUpdatedBefore": until.isoformat() + "Z"}
        if pipelines:
            body["filters"] = [{"operand": "PipelineName", "operator": "In", "values": pipelines}]
        runs: list[PipelineRun] = []
        while True:
            d = self._call("POST", f"{self._base}/queryPipelineRuns", json=body)
            runs += [self._run(x) for x in d.get("value", [])]
            token = d.get("continuationToken")
            if not token:
                return runs
            body["continuationToken"] = token

    def get_runs(self, run_ids: list[str]) -> dict[str, PipelineRun]:
        runs: dict[str, PipelineRun] = {}
        for run_id in run_ids:
            try:
                runs[run_id] = self._run(self._call("GET", f"{self._base}/pipelineruns/{run_id}"))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        return runs

    def get_activity_runs(self, run_id: str) -> list[ActivityRun]:
        if "@" in run_id:  # not an ADF run id
            return []
        now = utcnow()
        body: dict[str, Any] = {
            "lastUpdatedAfter": (now - timedelta(days=45)).isoformat() + "Z",
            "lastUpdatedBefore": (now + timedelta(minutes=5)).isoformat() + "Z",
        }
        acts: list[ActivityRun] = []
        while True:
            d = self._call("POST", self._activity_runs_url(run_id), json=body)
            for a in d.get("value", []):
                err = _json(a.get("error")) or {}
                acts.append(ActivityRun(
                    run_id=run_id,
                    name=a.get("activityName", ""),
                    type=a.get("activityType", ""),
                    status=a.get("status", ""),
                    started_at=_dt(a.get("activityRunStart")),
                    ended_at=_dt(a.get("activityRunEnd")),
                    duration_s=_seconds(a.get("durationInMs")),
                    output=_json(a.get("output")) or {},
                    error=err if isinstance(err, dict) and err.get("message") else None,
                    input=_json(a.get("input")) or {},
                ))
            token = d.get("continuationToken")
            if not token:
                break
            body["continuationToken"] = token
        acts.sort(key=lambda a: a.started_at or datetime.min)
        return acts

    def _activity_runs_url(self, run_id: str) -> str:
        return f"{self._base}/pipelineruns/{run_id}/queryActivityruns"

    # ── where data lands: dataset → table, per copy step ──
    def _linked_services(self) -> dict[str, dict[str, Any]]:
        """Connection names → database (non-secret fields only; connection strings and credentials are never read)."""
        if self._linked is None:
            out: dict[str, dict[str, Any]] = {}
            url: str | None = f"{self._base}/linkedservices"
            while url:
                d = self._call("GET", url)
                for ls in d.get("value", []):
                    tp = (ls.get("properties") or {}).get("typeProperties") or {}
                    kind = (ls.get("properties") or {}).get("type", "")
                    out[ls["name"]] = {"type": kind, "platform": PLATFORMS.get(kind, kind or "Dataset"), "host": _host_of(tp),
                                       "database": _plain(tp.get("database")) or _plain(tp.get("databaseName")) or _plain(tp.get("dbName")),
                                       "function_app_url": _plain(tp.get("functionAppUrl"))}
                url = d.get("nextLink")
            self._linked = out
        return self._linked

    def dataset_target(self, name: str) -> dict[str, Any]:
        """What a dataset points at: a table (DB.SCHEMA.TABLE when the connection names its database) or a folder."""
        if name not in self._datasets:
            try:
                p = self._call("GET", f"{self._base}/datasets/{name}").get("properties") or {}
            except Exception:  # noqa: BLE001 - unknown datasets fall back to their name
                p = {}
            tp = p.get("typeProperties") or {}
            ls = (p.get("linkedServiceName") or {}).get("referenceName", "")
            try:
                conn = self._linked_services().get(ls, {})
            except Exception:  # noqa: BLE001
                conn = {}
            db = conn.get("database")
            table = tp.get("table") or tp.get("tableName")
            dynamic = any(isinstance(x, dict) for x in (tp.get("schema"), table, tp.get("location")))
            if table:
                parts = [x for x in (db, tp.get("schema"), table) if isinstance(x, str) and x]
                parts = [x.replace("[", "").replace("]", "").replace('"', "") for x in parts]
                target = {"kind": "table", "table": ".".join(parts).upper() if not dynamic else name.upper(), "database": db}
            elif tp.get("location"):
                loc = tp["location"]
                bits = [_plain(loc.get("container")) or _plain(loc.get("fileSystem")) or _plain(loc.get("bucketName")), _plain(loc.get("folderPath"))]
                dynamic = dynamic or any(isinstance(loc.get(k), dict) for k in ("container", "fileSystem", "folderPath"))
                path = "/".join(x for x in bits if x)
                target = {"kind": "files", "table": path or name, "database": None, "file": _plain(loc.get("fileName"))}
            else:
                target = {"kind": "dataset", "table": name.upper(), "database": db}
            env = _env_of(f"{db or ''} {name}")
            self._datasets[name] = {**target, "dataset": name, "linked_service": ls, "env": env, "dynamic": dynamic,
                                    "platform": conn.get("platform") or (p.get("type") or "Dataset").removesuffix("Table").removesuffix("Location"),
                                    "host": conn.get("host")}
        return self._datasets[name]

    def step_targets(self, pipeline: str) -> dict[str, dict[str, Any]]:
        """Copy step name → the table (or folder) it writes, for every copy step in the pipeline (incl. nested)."""
        out: dict[str, dict[str, Any]] = {}

        def walk(nodes: list[dict[str, Any]]) -> None:
            for n in nodes:
                if n["type"] == "Copy" and n.get("writes"):
                    out[n["name"]] = self.dataset_target(n["writes"][0])
                walk(n["children"])

        walk(self._definition(pipeline))
        return out

    # ── failure tree: the pipeline as authored, with what actually ran attached to each step ──
    def _definition(self, pipeline: str) -> list[dict[str, Any]]:
        if pipeline not in self._definitions:
            d = self._call("GET", f"{self._base}/pipelines/{pipeline}")
            acts = (d.get("properties") or {}).get("activities") or []
            self._raw.setdefault(pipeline, acts)
            self._definitions[pipeline] = [_static_node(a) for a in acts]
        return self._definitions[pipeline]

    def run_tree(self, pipeline: str, run_id: str, depth: int = 0) -> list[dict[str, Any]]:
        """Every authored step (inside ForEach / If / Switch / Until, and child pipelines run by ExecutePipeline)
        with its status, iterations, timing, error and copy stats, so a failure can be followed down to the exact step."""
        by_name: dict[str, list[ActivityRun]] = {}
        for a in self.get_activity_runs(run_id):
            by_name.setdefault(a.name, []).append(a)
        return [self._attach(n, by_name, depth) for n in self._definition(pipeline)]

    def _attach(self, node: dict[str, Any], by_name: dict[str, list[ActivityRun]], depth: int) -> dict[str, Any]:
        runs = by_name.get(node["name"], [])
        statuses = [r.status for r in runs]
        status = ("NotRun" if not runs else "Failed" if "Failed" in statuses else "InProgress" if "InProgress" in statuses
                  else "Succeeded" if all(s == "Succeeded" for s in statuses) else statuses[-1])
        out: dict[str, Any] = {
            "name": node["name"], "type": node["type"], "branch": node["branch"], "status": status,
            "reads": node.get("reads", []), "writes": node.get("writes", []), "depends_on": node.get("depends_on", []),
            "iterations": len(runs),
            "runs": [_activity_dict(r) for r in runs[:50]],
            "children": [self._attach(c, by_name, depth) for c in node["children"]],
        }
        if node["calls"] and depth < 4:
            called = []
            for r in runs:
                child_run = (r.output or {}).get("pipelineRunId")
                if not child_run:
                    continue
                try:
                    called.append({"pipeline": node["calls"], "run_id": child_run,
                                   "steps": self.run_tree(node["calls"], child_run, depth + 1)})
                except Exception as exc:  # noqa: BLE001 - a child we can't read shouldn't hide the parent's tree
                    called.append({"pipeline": node["calls"], "run_id": child_run, "error": str(exc)[:300]})
            out["called"] = called
        return out


# Data Factory connection types → the platform people know them as
PLATFORMS = {
    "SnowflakeV2": "Snowflake", "Snowflake": "Snowflake", "AzureSqlDatabase": "Azure SQL", "AzureSqlMI": "Azure SQL MI", "SqlServer": "SQL Server",
    "AzureSqlDW": "Synapse SQL", "AzureSynapseAnalytics": "Synapse SQL", "AzureBlobStorage": "Blob Storage", "AzureBlobFS": "Data Lake",
    "AzureDataLakeStore": "Data Lake Gen1", "AzureFileStorage": "Azure Files", "FileServer": "File share", "Sftp": "SFTP", "FtpServer": "FTP",
    "AmazonS3": "Amazon S3", "AmazonS3Compatible": "S3", "GoogleCloudStorage": "Google Cloud Storage", "Oracle": "Oracle", "PostgreSql": "PostgreSQL",
    "PostgreSqlV2": "PostgreSQL", "AzurePostgreSql": "Azure PostgreSQL", "MySql": "MySQL", "AzureMySql": "Azure MySQL", "MariaDB": "MariaDB",
    "AzureDatabricks": "Databricks", "AzureDatabricksDeltaLake": "Databricks", "CosmosDb": "Cosmos DB", "CosmosDbMongoDbApi": "Cosmos DB",
    "MongoDbV2": "MongoDB", "MongoDbAtlas": "MongoDB Atlas", "RestService": "REST API", "HttpServer": "HTTP", "OData": "OData",
    "Salesforce": "Salesforce", "SalesforceV2": "Salesforce", "Dynamics": "Dynamics 365", "DynamicsCrm": "Dynamics 365", "CommonDataServiceForApps": "Dataverse",
    "SapTable": "SAP", "SapHana": "SAP HANA", "SapOdp": "SAP", "Teradata": "Teradata", "Db2": "DB2", "GoogleBigQuery": "BigQuery",
    "GoogleBigQueryV2": "BigQuery", "AmazonRedshift": "Redshift", "Odbc": "ODBC", "SharePointOnlineList": "SharePoint", "Office365": "Office 365",
    "AzureDataExplorer": "Data Explorer", "AzureTableStorage": "Table Storage", "AzureFunction": "Function App", "Hive": "Hive", "Spark": "Spark",
    "Netezza": "Netezza", "Vertica": "Vertica", "Greenplum": "Greenplum", "Informix": "Informix", "Sybase": "Sybase",
}
_HOST_FIELDS = ("accountIdentifier", "server", "host", "serviceEndpoint", "url", "baseUrl", "endpoint", "domain", "workspaceUrl", "accountName", "clusterUri")


def _host_of(tp: dict[str, Any]) -> str | None:
    """The server / account / endpoint a connection points at, from non-secret fields only (never a connection string)."""
    for k in _HOST_FIELDS:
        v = _plain(tp.get(k))
        if v:
            v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v.strip(), flags=re.I)
            v = v.split("/")[0].split("?")[0].split("@")[-1]
            return v.split(":")[0].lower() or None
    return None


_LOGIC_URL = re.compile(r"https://([a-z0-9.-]+\.logic\.azure\.com)(?::\d+)?/workflows/([0-9a-f]{32})", re.I)
_FUNC_HOST = re.compile(r"https://([a-z0-9-]+)\.azurewebsites\.net", re.I)


def _plain(v: Any) -> str | None:
    """A literal string value (not an expression or a secure-string reference)."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and v.get("type") == "Expression":
        return None
    return None


def _walk(acts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for a in acts or []:
        out.append(a)
        out += _walk([x for x, _ in _static_children(a)])
    return out


def connections(acts: list[dict[str, Any]], linked: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """What a pipeline calls outside itself: Logic Apps, Function Apps, other pipelines, notebooks, web endpoints.
    Only names and ids are kept; a Logic App's trigger URL carries a signature, so everything after the path is dropped."""
    out: dict[tuple[str, str], dict[str, Any]] = {}

    def add(kind: str, name: str, step: str, **extra: Any) -> None:
        cur = out.setdefault((kind, name), {"kind": kind, "name": name, "steps": [], **extra})
        if step not in cur["steps"]:
            cur["steps"].append(step)

    for a in _walk(acts):
        t, tp, step = a.get("type", ""), a.get("typeProperties") or {}, a.get("name", "")
        if t == "AzureFunctionActivity":
            ls = (a.get("linkedServiceName") or {}).get("referenceName", "")
            url = (linked.get(ls) or {}).get("function_app_url") or ""
            m = _FUNC_HOST.search(url)
            fn = _plain(tp.get("functionName"))
            add("function_app", m.group(1) if m else ls or "Function App", step, function=fn)
        elif t in ("WebActivity", "WebHook"):
            url = _plain(tp.get("url")) or ""
            if m := _LOGIC_URL.search(url):
                add("logic_app", m.group(2).lower(), step, workflow_id=m.group(2).lower())
            elif m := _FUNC_HOST.search(url):
                add("function_app", m.group(1), step)
            elif url.startswith("http"):
                add("web", url.split("/")[2].split("?")[0], step)
        elif t == "ExecutePipeline":
            add("pipeline", (tp.get("pipeline") or {}).get("referenceName", ""), step)
        elif t == "SqlServerStoredProcedure" and (proc := _plain(tp.get("storedProcedureName"))):
            add("procedure", proc, step, linked_service=(a.get("linkedServiceName") or {}).get("referenceName", ""))
        elif t.startswith("Databricks"):
            add("databricks", (a.get("linkedServiceName") or {}).get("referenceName", "Databricks"), step)
        elif t in ("SynapseNotebook", "SparkJob"):
            add("notebook", ((tp.get("notebook") or tp.get("sparkJob") or {}).get("referenceName")) or step, step)
    return list(out.values())


_TRIGGER_KIND = {"ScheduleTrigger": "Schedule", "TumblingWindowTrigger": "Tumbling window", "BlobEventsTrigger": "Storage event",
                 "CustomEventsTrigger": "Custom event"}


def _error_text(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return str(err.get("message") or err.get("code") or r.text)[:300]
    except ValueError:
        return r.text[:300]


def _env_of(text: str) -> str | None:
    t = text.upper()
    for env, keys in (("PROD", ("DB_PROD", "_PROD", "PROD")), ("UAT", ("DB_UAT", "UAT")), ("DEV", ("DB_DEV", "_DEV", "DEV"))):
        if any(k in t for k in keys):
            return env
    return None


def _static_children(activity: dict[str, Any]) -> list[tuple[dict[str, Any], str | None]]:
    tp = activity.get("typeProperties") or {}
    kids: list[tuple[dict[str, Any], str | None]] = [(a, None) for a in tp.get("activities") or []]
    kids += [(a, "If true") for a in tp.get("ifTrueActivities") or []]
    kids += [(a, "If false") for a in tp.get("ifFalseActivities") or []]
    kids += [(a, "Default") for a in tp.get("defaultActivities") or []]
    for case in tp.get("cases") or []:
        kids += [(a, f"Case {case.get('value')}") for a in case.get("activities") or []]
    return kids


def _static_node(activity: dict[str, Any], branch: str | None = None) -> dict[str, Any]:
    tp = activity.get("typeProperties") or {}
    return {
        "name": activity.get("name", ""),
        "type": activity.get("type", ""),
        "branch": branch,
        "calls": (tp.get("pipeline") or {}).get("referenceName") if activity.get("type") == "ExecutePipeline" else None,
        "reads": [d.get("referenceName") for d in activity.get("inputs") or [] if d.get("referenceName")],
        "writes": [d.get("referenceName") for d in activity.get("outputs") or [] if d.get("referenceName")],
        # the arrows in ADF Studio: which steps this one waits for, and on what outcome
        "depends_on": [{"activity": d.get("activity"), "conditions": d.get("dependencyConditions") or ["Succeeded"]}
                       for d in activity.get("dependsOn") or [] if d.get("activity")],
        "children": [_static_node(a, b) for a, b in _static_children(activity)],
    }


def copy_stats(output: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(output, dict) or not ({"rowsCopied", "rowsRead", "dataRead"} & output.keys()):
        return None
    return {"rows_read": output.get("rowsRead"), "rows_copied": output.get("rowsCopied"),
            "rows_skipped": output.get("rowsSkipped"), "data_read": output.get("dataRead"),
            "data_written": output.get("dataWritten"), "copy_duration_s": output.get("copyDuration"),
            "throughput_kbps": output.get("throughput")}


def _step_facts(a: ActivityRun) -> list[str]:
    """One-line facts a person looks for in ADF Studio's Input / Output tabs, per step type."""
    out, inp = a.output or {}, a.input or {}
    facts: list[str] = []
    store = ((inp.get("source") or {}).get("storeSettings") or {})
    folder = store.get("wildcardFolderPath") or store.get("folderPath")
    if a.type == "GetMetadata":
        kids = [c.get("name") for c in out.get("childItems") or []]
        if out.get("itemName"):
            facts.append(f"Folder {out['itemName']}")
        if "childItems" in out:
            facts.append(f"{len(kids)} item(s): {', '.join(kids[:12])}" + ("…" if len(kids) > 12 else "") if kids else "Folder is empty")
    elif a.type == "Filter":
        if "FilteredItemsCount" in out:
            facts.append(f"Kept {out['FilteredItemsCount']} of {out.get('ItemsCount')} item(s)")
        if inp.get("condition"):
            facts.append(f"Condition {str(inp['condition']).lstrip('@')}")
        kept = [v.get("name") for v in out.get("Value") or [] if isinstance(v, dict)]
        if kept:
            facts.append("Kept: " + ", ".join(kept[:8]) + ("…" if len(kept) > 8 else ""))
    elif a.type == "Copy":
        if folder:
            facts.append(f"Reads {folder}/{store.get('wildcardFileName') or '*'}")
        sink = ((inp.get("sink") or {}).get("type") or "").removesuffix("Sink").removesuffix("V2")
        if sink:
            facts.append(f"Writes to {sink}")
        if out.get("rowsCopied") is not None:
            facts.append(f"{out.get('rowsRead', out['rowsCopied']):,} rows read · {out['rowsCopied']:,} copied"
                         + (f" · {out['rowsSkipped']:,} skipped" if out.get("rowsSkipped") else ""))
        elif out.get("filesWritten") is not None or out.get("filesRead") is not None:
            facts.append(f"{out.get('filesRead', 0)} file(s) read · {out.get('filesWritten', 0)} written")
    elif a.type == "Delete":
        if out.get("filesDeleted") is not None:
            facts.append(f"Deleted {out['filesDeleted']} file(s)" + (f" from {out['folderPath']}" if out.get("folderPath") else ""))
    elif a.type == "Lookup":
        if out.get("count") is not None:
            facts.append(f"Returned {out['count']} row(s)")
    return facts


def _activity_dict(a: ActivityRun) -> dict[str, Any]:
    return {"status": a.status, "start": iso(a.started_at), "end": iso(a.ended_at), "duration_s": a.duration_s,
            "error": (a.error or {}).get("message") or None, "error_code": (a.error or {}).get("errorCode") or None,
            "copy": copy_stats(a.output), "facts": _step_facts(a)}


def failure_path(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The chain of steps from the top of the pipeline down to the deepest failed step (with its error)."""
    for node in tree:
        if node["status"] != "Failed":
            continue
        deeper = failure_path(node["children"])
        for call in node.get("called") or []:
            deeper = deeper or failure_path(call.get("steps") or [])
        failed_runs = [r for r in node["runs"] if r["status"] == "Failed"]
        here = {"name": node["name"], "type": node["type"], "iteration": None, "of": node["iterations"],
                "error": failed_runs[-1]["error"] if failed_runs else None,
                "error_code": failed_runs[-1]["error_code"] if failed_runs else None}
        if node["iterations"] > 1 and failed_runs:
            here["iteration"] = node["runs"].index(failed_runs[0]) + 1
        return [here, *deeper]
    return []


class FakeAdfPipeline:
    """Reads `_adf_pipeline_runs` / `_adf_activity_runs` written by the simulator."""

    def __init__(self, cfg: PipelineConfig, src: SourceConfig):
        self.cfg = cfg
        self.engine = engine_for(src)
        self.runs = sa.table(
            "_adf_pipeline_runs",
            *(sa.column(c) for c in ("run_id", "pipeline_name", "status", "run_start", "run_end", "duration_ms", "parameters", "message")),
        )
        self.acts = sa.table(
            "_adf_activity_runs",
            *(sa.column(c) for c in ("run_id", "activity_name", "activity_type", "status", "start", "end", "duration_ms", "output", "error")),
        )

    def get_runs(self, run_ids: list[str]) -> dict[str, PipelineRun]:
        if not run_ids:
            return {}
        out: dict[str, PipelineRun] = {}
        with self.engine.connect() as conn:
            for chunk in (run_ids[i : i + 500] for i in range(0, len(run_ids), 500)):
                for r in conn.execute(sa.select(self.runs).where(self.runs.c.run_id.in_(chunk))).mappings():
                    out[r["run_id"]] = PipelineRun(
                        run_id=r["run_id"],
                        pipeline=r["pipeline_name"],
                        status=r["status"],
                        started_at=_dt(r["run_start"]),
                        ended_at=_dt(r["run_end"]),
                        duration_s=_seconds(r["duration_ms"]),
                        parameters=_json(r["parameters"]) or {},
                        message=r["message"] or "",
                    )
        return out

    def get_activity_runs(self, run_id: str) -> list[ActivityRun]:
        stmt = sa.select(self.acts).where(self.acts.c.run_id == run_id).order_by(self.acts.c.start)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [
            ActivityRun(
                run_id=r["run_id"],
                name=r["activity_name"],
                type=r["activity_type"],
                status=r["status"],
                started_at=_dt(r["start"]),
                ended_at=_dt(r["end"]),
                duration_s=_seconds(r["duration_ms"]),
                output=_json(r["output"]) or {},
                error=_json(r["error"]) or None,
            )
            for r in rows
        ]
