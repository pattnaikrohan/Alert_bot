"""Warehouse cost: tables, views, stored procedures and functions nobody has used for months, what keeping them costs,
and the script to remove them. (Pipelines nobody runs are in dq/idle.py.)

How "unused" is decided (it has to be true, or someone drops something live):
    SQL Server   tables      sys.dm_db_index_usage_stats: the last read (seek / scan / lookup) and write of every table.
                 views       a view is read through its tables, so it can't have been queried if none of its tables were
                             read (high confidence); views on tables in other databases can't be judged.
                 procedures  sys.dm_exec_procedure_stats (and dm_exec_function_stats for scalar functions): when each was
                 functions   last executed. SQL Server also drops these when a plan leaves memory, so they're "medium"
                             confidence; the bot scans hourly and keeps what it saw.
                 All of these empty on a restart, so the bot keeps its own record (a restart doesn't wipe what it saw).
                 Times are server-local, converted with the server's own UTC offset.
    Snowflake    ACCOUNT_USAGE.ACCESS_HISTORY: a year of exact table and view reads and writes (Enterprise edition, with
                 access to the SNOWFLAKE database); procedures from QUERY_HISTORY's CALL statements (only the name is
                 taken, worked out inside Snowflake: no query text reaches the bot). Without it, only a table's last
                 change (LAST_ALTERED) is known: reads can't be seen.
Something is "ready to remove" only when nothing used it for at least the threshold, over a period the bot can
actually see. Shorter evidence is "watching" (certain in N days); what the pipelines load, read or call is never
suggested.

Read-only, metadata only (system views: names, sizes, times): no table's rows are ever read. The bot never drops
anything: it writes the script, and a person runs it.
"""

from __future__ import annotations

import logging
import re
import textwrap
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import BotState, DbConnection, DqMapping, Project, User
from ..timeutil import iso, utcnow
from . import sql as T
from .client import DbError
from .privacy import RawDataRejected, safe_name, safe_type

log = logging.getLogger(__name__)

SCAN_EVERY = timedelta(hours=1)
RESTART_GAP = timedelta(hours=2)  # a restart this soon after a scan loses (almost) nothing the bot hadn't already recorded
KEY = "storage:conn:{}"
GB = 1024 ** 3
STATUSES = ("unused", "watching", "write_only", "maybe", "in_use", "kept", "unknown")

# List prices per GB-month, editable per database (contracts, regions and tiers differ).
PRICES = {
    "sqlserver": (0.115, "Azure SQL Database, General Purpose data storage (US list price)"),
    "snowflake": (0.040, "Snowflake on-demand storage, about $40 per TB-month (US; about $23 on a capacity contract)"),
    "sqlite": (0.0, "local file"),
}

# SQL Server object types: (what people call it, OBJECT_ID type, DROP keyword)
SQL_TYPES = {"V": ("view", "V", "VIEW"), "P": ("procedure", "P", "PROCEDURE"), "FN": ("function", "FN", "FUNCTION"),
             "IF": ("function", "IF", "FUNCTION"), "TF": ("function", "TF", "FUNCTION")}

# ── the metadata queries (single SELECTs, through the read-only guard) ────────


def _q(name: str, sql: str, params: dict[str, Any] | None = None) -> T.MetaQuery:
    return T.MetaQuery(name, sql, params or {})


def queries(kind: str, database: str) -> dict[str, T.MetaQuery]:
    if kind == "sqlserver":
        stats = ("SELECT OBJECT_SCHEMA_NAME(x.object_id) AS schema_name, OBJECT_NAME(x.object_id) AS object_name, x.last_run, x.runs FROM "
                 "(SELECT object_id, MAX(last_execution_time) AS last_run, SUM(execution_count) AS runs FROM sys.{view} "
                 "WHERE database_id = DB_ID() GROUP BY object_id) x")
        return {
            "clock": _q("storage_clock", "SELECT DATEDIFF(MINUTE, SYSUTCDATETIME(), SYSDATETIME()) AS offset_min"),
            "start": _q("storage_start", "SELECT sqlserver_start_time FROM sys.dm_os_sys_info"),
            "sizes": _q("storage_sizes",
                        "SELECT s.name AS schema_name, t.name AS table_name, t.create_date, t.modify_date, "
                        "(SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)) AS row_count, "
                        "(SELECT SUM(a.total_pages) FROM sys.partitions p JOIN sys.allocation_units a ON a.container_id = p.partition_id "
                        "WHERE p.object_id = t.object_id) * 8192 AS bytes "
                        "FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id WHERE t.is_ms_shipped = 0"),
            "usage": _q("storage_usage",
                        "SELECT s.name AS schema_name, t.name AS table_name, MAX(r.last_read) AS last_read, MAX(u.last_user_update) AS last_write "
                        "FROM sys.dm_db_index_usage_stats u JOIN sys.tables t ON t.object_id = u.object_id "
                        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
                        "CROSS APPLY (SELECT MAX(v) AS last_read FROM (VALUES (u.last_user_seek), (u.last_user_scan), (u.last_user_lookup)) AS x(v)) r "
                        "WHERE u.database_id = DB_ID() GROUP BY s.name, t.name"),
            "deps": _q("storage_deps",
                       "SELECT s.name, t.name, OBJECT_SCHEMA_NAME(d.referencing_id) AS ref_schema, OBJECT_NAME(d.referencing_id) AS ref_name, "
                       "'view or procedure' AS kind FROM sys.sql_expression_dependencies d JOIN sys.tables t ON t.object_id = d.referenced_id "
                       "JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE d.referencing_id <> d.referenced_id "
                       "UNION ALL SELECT s.name, t.name, OBJECT_SCHEMA_NAME(fk.parent_object_id), OBJECT_NAME(fk.parent_object_id), 'foreign key' "
                       "FROM sys.foreign_keys fk JOIN sys.tables t ON t.object_id = fk.referenced_object_id "
                       "JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE fk.parent_object_id <> fk.referenced_object_id"),
            "objects": _q("storage_objects",
                          "SELECT s.name AS schema_name, o.name AS object_name, o.type, o.create_date, o.modify_date FROM sys.objects o "
                          "JOIN sys.schemas s ON s.schema_id = o.schema_id WHERE o.is_ms_shipped = 0 AND o.type IN ('V', 'P', 'FN', 'IF', 'TF')"),
            "edges": _q("storage_edges",
                        "SELECT rs.name AS from_schema, ro.name AS from_name, ts.name AS to_schema, t.name AS to_name, t.type AS to_type "
                        "FROM sys.sql_expression_dependencies d JOIN sys.objects ro ON ro.object_id = d.referencing_id "
                        "JOIN sys.schemas rs ON rs.schema_id = ro.schema_id JOIN sys.objects t ON t.object_id = d.referenced_id "
                        "JOIN sys.schemas ts ON ts.schema_id = t.schema_id WHERE d.referencing_id <> d.referenced_id AND ro.is_ms_shipped = 0"),
            "proc_runs": _q("storage_proc_runs", stats.format(view="dm_exec_procedure_stats")),
            "func_runs": _q("storage_func_runs", stats.format(view="dm_exec_function_stats")),
        }
    if kind == "snowflake":
        prefix = f"{database.upper()}.%"
        flat = ("SELECT f.value['objectName']::string AS obj, MAX(h.query_start_time) AS at "
                "FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY h, LATERAL FLATTEN(input => h.{col}) f "
                "WHERE h.query_start_time >= DATEADD(day, -365, CURRENT_TIMESTAMP()) AND f.value['objectDomain']::string = :domain "
                "AND f.value['objectName']::string LIKE :prefix GROUP BY 1")
        return {
            "sizes": _q("storage_sizes",
                        "SELECT TABLE_SCHEMA, TABLE_NAME, CREATED, LAST_DDL, ROW_COUNT, BYTES, LAST_ALTERED, IS_TRANSIENT, RETENTION_TIME "
                        "FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_SCHEMA <> 'INFORMATION_SCHEMA'"),
            "reads": _q("storage_reads", flat.format(col="base_objects_accessed"), {"prefix": prefix, "domain": "Table"}),
            "writes": _q("storage_writes", flat.format(col="objects_modified"), {"prefix": prefix, "domain": "Table"}),
            "views": _q("storage_views", "SELECT TABLE_SCHEMA, TABLE_NAME, CREATED, LAST_ALTERED FROM INFORMATION_SCHEMA.VIEWS "
                                         "WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA'"),
            "procs": _q("storage_procs", "SELECT PROCEDURE_SCHEMA, PROCEDURE_NAME, ARGUMENT_SIGNATURE, CREATED, LAST_ALTERED "
                                         "FROM INFORMATION_SCHEMA.PROCEDURES WHERE PROCEDURE_SCHEMA <> 'INFORMATION_SCHEMA'"),
            "funcs": _q("storage_funcs", "SELECT FUNCTION_SCHEMA, FUNCTION_NAME, ARGUMENT_SIGNATURE, CREATED, LAST_ALTERED "
                                         "FROM INFORMATION_SCHEMA.FUNCTIONS WHERE FUNCTION_SCHEMA <> 'INFORMATION_SCHEMA'"),
            "view_reads": _q("storage_view_reads", flat.format(col="direct_objects_accessed"), {"prefix": prefix, "domain": "View"}),
            # only the procedure's name leaves Snowflake: the CALL statement's text is reduced to it there
            "calls": _q("storage_calls",
                        "SELECT UPPER(REGEXP_SUBSTR(QUERY_TEXT, 'CALL\\\\s+([A-Za-z0-9_$.\"]+)', 1, 1, 'ie', 1)) AS proc, MAX(START_TIME) AS at "
                        "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY WHERE QUERY_TYPE = 'CALL' AND DATABASE_NAME = :db "
                        "AND START_TIME >= DATEADD(day, -365, CURRENT_TIMESTAMP()) GROUP BY 1", {"db": database.upper()}),
        }
    return {"sizes": _q("storage_sizes", "SELECT 'main', name, NULL, NULL, NULL, NULL FROM sqlite_master "
                                         "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


# ── reading a database ────────────────────────────────────────────────────────


def _utc(v: Any, offset_min: int = 0) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, str):  # text with a zone is converted by it; without one, it's the server's local time
        try:
            parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return _dt(v)
        v = parsed
    if not isinstance(v, datetime):
        return None
    if v.tzinfo is not None:
        return (v - v.utcoffset()).replace(tzinfo=None)  # type: ignore[operator]
    return v - timedelta(minutes=offset_min)


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _names(*vals: Any) -> tuple[str, ...] | None:
    try:
        return tuple(safe_name(str(v)) for v in vals)
    except RawDataRejected:
        return None


def _arg_types(signature: Any) -> str | None:
    """Snowflake's "(ID NUMBER, NAME VARCHAR)" as "NUMBER, VARCHAR" (what DROP PROCEDURE needs); None if odd."""
    s = str(signature or "").strip()
    if not (s.startswith("(") and s.endswith(")")):
        return None
    parts = [p.strip() for p in s[1:-1].split(",") if p.strip()]
    try:
        return ", ".join(safe_type(p.split(None, 1)[1] if " " in p else p) for p in parts)
    except RawDataRejected:
        return None


def _try(client: Any, q: T.MetaQuery, limit: int) -> tuple[list[Any] | None, str | None]:
    try:
        return list(client.meta(q, limit)), None
    except DbError as exc:
        return None, str(exc)


def read(client: Any, conn: DbConnection) -> dict[str, Any]:
    """Everything the database says about its tables', views', procedures' and functions' size and use. The table
    listing must work; everything else is optional (a login without a permission still gets sizes, and is told what
    to grant)."""
    q = queries(conn.kind, conn.database)
    out: dict[str, Any] = {"tables": {}, "deps": {}, "usage": "none", "usage_error": None, "server_start": None,
                           "objects": {}, "object_usage": {}}
    offset = 0
    if "clock" in q:
        try:
            offset = int(_num(client.meta(q["clock"], 1)[0][0]) or 0)
        except (DbError, IndexError, TypeError):
            offset = 0
    for row in client.meta(q["sizes"], 20000):
        n = _names(row[0], row[1])
        if n is None:
            continue
        schema, table = n
        key = f"{schema}.{table}"
        if conn.kind == "snowflake":
            out["tables"][key] = {"schema": schema, "table": table, "created": iso(_utc(row[2])), "ddl": iso(_utc(row[3])),
                                  "rows": _num(row[4]), "bytes": _num(row[5]), "altered": iso(_utc(row[6])),
                                  "transient": str(row[7]).upper() == "YES", "retention_days": _num(row[8])}
        else:
            out["tables"][key] = {"schema": schema, "table": table, "created": iso(_utc(row[2], offset)), "ddl": iso(_utc(row[3], offset)),
                                  "rows": _num(row[4]), "bytes": _num(row[5])}
    if conn.kind == "sqlserver":
        _read_sqlserver(client, q, out, offset)
    elif conn.kind == "snowflake":
        _read_snowflake(client, q, out)
    return out


def _read_sqlserver(client: Any, q: dict[str, T.MetaQuery], out: dict[str, Any], offset: int) -> None:
    try:
        out["server_start"] = iso(_utc(client.meta(q["start"], 1)[0][0], offset))
    except (DbError, IndexError):
        out["server_start"] = None
    try:
        for schema, table, last_read, last_write in client.meta(q["usage"], 20000):
            key = f"{schema}.{table}"
            if key in out["tables"]:
                out["tables"][key].update(last_read=iso(_utc(last_read, offset)), last_write=iso(_utc(last_write, offset)))
        out["usage"] = "dmv"
    except DbError as exc:
        out["usage_error"] = str(exc)
    rows, _ = _try(client, q["deps"], 5000)
    for schema, table, ref_schema, ref_name, kind in rows or []:
        out["deps"].setdefault(f"{schema}.{table}", []).append({"object": f"{ref_schema}.{ref_name}", "kind": kind})
    objs, _ = _try(client, q["objects"], 20000)
    for schema, name, code, created, altered in objs or []:
        n = _names(schema, name)
        code = str(code).strip().upper()
        if n is None or code not in SQL_TYPES:
            continue
        out["objects"][f"{n[0]}.{n[1]}"] = {"schema": n[0], "name": n[1], "type": SQL_TYPES[code][0], "code": code,
                                            "created": iso(_utc(created, offset)), "altered": iso(_utc(altered, offset)),
                                            "last_used": None, "uses": [], "used_by": []}
    edges, _ = _try(client, q["edges"], 50000)
    for fs, fn, ts, tn, _type in edges or []:
        src, dst = f"{fs}.{fn}", f"{ts}.{tn}"
        if src in out["objects"]:
            out["objects"][src]["uses"].append(dst)
        if dst in out["objects"]:
            out["objects"][dst]["used_by"].append(src)
    seen = {"views": "base_tables" if out["usage"] == "dmv" else None}
    for which, label in (("proc_runs", "procedures"), ("func_runs", "functions")):
        rows, err = _try(client, q[which], 20000)
        seen[label] = "plan_cache" if rows is not None else None
        for schema, name, last_run, _runs in rows or []:
            key = f"{schema}.{name}"
            if key in out["objects"]:
                out["objects"][key]["last_used"] = iso(_utc(last_run, offset))
    out["object_usage"] = seen


def _read_snowflake(client: Any, q: dict[str, T.MetaQuery], out: dict[str, Any]) -> None:
    upper = {k.upper(): k for k in out["tables"]}
    try:
        for which in ("reads", "writes"):
            for obj, at in client.meta(q[which], 50000):
                parts = str(obj).split(".")
                if len(parts) == 3 and (key := f"{parts[1]}.{parts[2]}".upper()) in upper:
                    out["tables"][upper[key]]["last_read" if which == "reads" else "last_write"] = iso(_utc(at))
        out["usage"] = "access_history"
    except DbError as exc:
        out["usage"], out["usage_error"] = "last_altered", str(exc)
    for which, kind in (("views", "view"), ("procs", "procedure"), ("funcs", "function")):
        rows, _ = _try(client, q[which], 20000)
        for row in rows or []:
            n = _names(row[0], row[1])
            if n is None:
                continue
            key = f"{n[0]}.{n[1]}"
            sig = _arg_types(row[2]) if kind != "view" else None
            o = out["objects"].setdefault(key, {"schema": n[0], "name": n[1], "type": kind, "code": kind,
                                                "created": iso(_utc(row[-2])), "altered": iso(_utc(row[-1])), "last_used": None,
                                                "uses": [], "used_by": [], "signatures": []})
            if kind != "view" and sig is not None and sig not in o["signatures"]:
                o["signatures"].append(sig)  # overloads: one DROP each
    seen: dict[str, str | None] = {"views": None, "procedures": None, "functions": None}
    objs = {k.upper(): k for k in out["objects"]}
    rows, _ = _try(client, q["view_reads"], 50000)
    if rows is not None and out["usage"] == "access_history":
        seen["views"] = "access_history"
        for obj, at in rows:
            parts = str(obj).split(".")
            if len(parts) == 3 and (key := f"{parts[1]}.{parts[2]}".upper()) in objs:
                out["objects"][objs[key]]["last_used"] = iso(_utc(at))
    rows, _ = _try(client, q["calls"], 50000)
    if rows is not None:
        seen["procedures"] = "query_history"
        for name, at in rows:
            parts = str(name or "").replace('"', "").upper().split(".")
            key = ".".join(parts[-2:]) if len(parts) >= 2 else None
            if key and key in objs and out["objects"][objs[key]]["type"] == "procedure":
                o = out["objects"][objs[key]]
                o["last_used"] = max(x for x in (o["last_used"], iso(_utc(at))) if x)
    out["object_usage"] = seen


# ── the bot's own memory of usage (a restart doesn't erase it) ────────────────


def _dt(s: str) -> datetime:
    """A stored time (ISO, UTC, maybe with "Z") as a naive UTC datetime, like the bot's own clock."""
    v = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (v - v.utcoffset()).replace(tzinfo=None) if v.tzinfo else v


def _later(a: str | None, b: str | None) -> str | None:
    return max((x for x in (a, b) if x), default=None)


def merge(prev: dict[str, Any] | None, now_read: dict[str, Any], now: datetime) -> dict[str, Any]:
    prev = prev or {}
    seen = prev.get("tables") or {}
    tables = {}
    for key, t in now_read["tables"].items():
        old = seen.get(key) or {}
        tables[key] = {**t, "first_seen": old.get("first_seen") or iso(now),
                       "last_read": _later(t.get("last_read"), old.get("last_read")),
                       "last_write": _later(t.get("last_write"), old.get("last_write"))}
    seen_objs = prev.get("objects") or {}
    objects = {}
    for key, o in (now_read.get("objects") or {}).items():
        old = seen_objs.get(key) or {}
        objects[key] = {**o, "first_seen": old.get("first_seen") or iso(now), "last_used": _later(o.get("last_used"), old.get("last_used"))}
    usage = now_read["usage"]
    covered = prev.get("covered_since")
    if usage == "dmv":
        start = now_read.get("server_start")
        last_scan = prev.get("scanned_at") if prev.get("usage") == "dmv" else None
        if covered is None or last_scan is None:
            covered = start or iso(now)  # the engine has counted since it started
        elif start and _dt(start) > _dt(last_scan) + RESTART_GAP:
            covered = start  # restarted after a long unwatched spell: only trust from the restart
    elif usage == "access_history":
        covered = iso(now - timedelta(days=365))
    else:
        covered = None
    return {"scanned_at": iso(now), "usage": usage, "usage_error": now_read.get("usage_error"), "server_start": now_read.get("server_start"),
            "covered_since": covered, "tables": tables, "deps": now_read.get("deps") or {},
            "objects": objects, "object_usage": now_read.get("object_usage") or {},
            "keep": prev.get("keep") or {}, "price": prev.get("price"), "error": None}


def scan(session: Session, conn: DbConnection, now: datetime | None = None) -> dict[str, Any]:
    """Read the database now (no write held meanwhile), then record what it said in one quick step."""
    from .service import client_of

    now = now or utcnow()
    key = KEY.format(conn.id)
    try:
        got = read(client_of(conn), conn)
        error = None
    except (DbError, Exception) as exc:  # noqa: BLE001 - shown on the page; the last good scan stays
        got, error = None, str(exc)[:300] if isinstance(exc, DbError) else "the database couldn't be read just now"
    row = session.get(BotState, key) or BotState(key=key, value={})
    prev = dict(row.value or {})
    state = {**prev, "error": error, "error_at": iso(now)} if got is None else merge(prev, got, now)
    row.value, row.updated_at = state, now
    session.add(row)
    session.flush()
    return state


def state_of(session: Session, conn_id: int) -> dict[str, Any]:
    row = session.get(BotState, KEY.format(conn_id))
    return dict(row.value) if row and row.value else {}


def save_state(session: Session, conn_id: int, state: dict[str, Any]) -> None:
    row = session.get(BotState, KEY.format(conn_id)) or BotState(key=KEY.format(conn_id), value={})
    row.value, row.updated_at = state, utcnow()
    session.add(row)
    session.flush()


def price_of(conn: DbConnection, state: dict[str, Any]) -> tuple[float, str, bool]:
    if state.get("price") is not None:
        return float(state["price"]), "set by you", True
    p, note = PRICES.get(conn.kind, (0.0, ""))
    return p, note, False


# ── what's in use (never suggested) ───────────────────────────────────────────


def protected(session: Session, conn: DbConnection) -> dict[str, list[str]]:
    """Tables and views on this database the pipelines load or read (from their definitions), procedures they call,
    and tables with data checks: SCHEMA.NAME in capitals → who uses it."""
    from ..engine.adf import inventory_of
    from .service import _matches, canonical

    out: dict[str, list[str]] = {}
    for p in session.scalars(select(Project).where(Project.active.is_(True))):
        for item in inventory_of(session, p.key).get("items") or []:
            for step in item.get("lineage") or []:
                for t in (step.get("writes") or []) + (step.get("reads") or []):
                    if t.get("kind") == "table" and not t.get("dynamic") and _matches(conn, t):
                        parts = str(t["table"]).upper().split(".")
                        out.setdefault(f"{parts[1]}.{parts[2]}", []).append(f"pipeline {item['name']}")
            for c in item.get("connections") or []:
                if c.get("kind") == "procedure" and c.get("name"):
                    # matched by name only (the step's server isn't always known): erring on the side of keeping it
                    parts = [x.strip("[]\" ") for x in str(c["name"]).upper().split(".") if x.strip("[]\" ")]
                    key = ".".join(parts[-2:]) if len(parts) >= 2 else f"{'DBO' if conn.kind == 'sqlserver' else 'PUBLIC'}.{parts[0]}" if parts else None
                    if key:
                        out.setdefault(key, []).append(f"pipeline {item['name']}")
    for m in session.scalars(select(DqMapping).where(DqMapping.connection_id == conn.id, DqMapping.retired_at.is_(None))):
        out.setdefault(canonical(conn, m.schema_name, m.table_name).split(".", 1)[1], []).append("data checks")
    return {k: sorted(set(v)) for k, v in out.items()}


# ── deciding ──────────────────────────────────────────────────────────────────


def _days(a: datetime, b: str | None) -> float | None:
    return (a - _dt(b)).total_seconds() / 86400 if b else None


def classify(t: dict[str, Any], state: dict[str, Any], kind: str, now: datetime, days: int, guarded: list[str], kept: dict[str, Any] | None) -> dict[str, Any]:
    usage = state.get("usage")
    covered = state.get("covered_since")
    start = max((x for x in (covered, t.get("created")) if x), default=None) if covered else None
    seen_days = _days(now, start)
    last_read, last_write = t.get("last_read"), t.get("last_write")
    last_use = _later(last_read, last_write)
    idle_since = last_use or start
    idle = _days(now, idle_since)
    read_idle = _days(now, last_read or start)
    out: dict[str, Any] = {"seen_days": round(seen_days, 1) if seen_days is not None else None, "idle_days": round(idle, 1) if idle is not None else None,
                           "since": idle_since, "sure_in_days": None}
    if kept:
        out.update(status="kept", why=f"Kept by {kept.get('by')}" + (f": {kept['reason']}" if kept.get("reason") else ""))
    elif guarded:
        out.update(status="in_use", why="Used by " + ", ".join(guarded))
    elif usage in ("dmv", "access_history") and seen_days is not None:
        if last_use and idle is not None and idle < days:  # a real read or write lately (being created isn't a use)
            if read_idle is not None and read_idle >= days and seen_days >= days:
                out.update(status="write_only", why=f"Written {_ago(idle)} ago but not read for {_ago(read_idle)}")
            else:
                out.update(status="in_use", why=f"Used {_ago(idle)} ago")
        elif seen_days >= days:
            out.update(status="unused", confidence="high",
                       why=("Never read or written" if not last_use else f"Last used {_ago(idle)} ago") + f" (watched for {_ago(seen_days)})")
        else:
            out.update(status="watching", sure_in_days=round(days - seen_days, 1),
                       why=f"No use in the {_ago(seen_days)} the bot can see; certain in {_ago(days - seen_days)}")
    elif kind == "snowflake" and t.get("altered"):
        changed = _days(now, t["altered"]) or 0
        out["idle_days"], out["since"] = round(changed, 1), t["altered"]
        out.update(status="maybe" if changed >= days else "in_use", confidence="low",
                   why=f"Not changed for {_ago(changed)}; reads can't be seen" if changed >= days else f"Changed {_ago(changed)} ago")
    else:
        out.update(status="unknown", why="Usage can't be seen with this login")
    return out


def _through_tables(key: str, objects: dict[str, Any], tables: dict[str, Any], depth: int = 0) -> tuple[str | None, int]:
    """The latest read of any table a view reads (through other views too), and how many of its tables are known."""
    if depth > 8:
        return None, 0
    latest, known = None, 0
    for used in objects.get(key, {}).get("uses") or []:
        if used in tables:
            known += 1
            latest = _later(latest, tables[used].get("last_read"))
        elif used in objects and objects[used]["type"] == "view":
            last, k = _through_tables(used, objects, tables, depth + 1)
            known += k
            latest = _later(latest, last)
    return latest, known


EVIDENCE = {
    "base_tables": ("high", "its tables"),
    "plan_cache": ("medium", "SQL Server's execution statistics"),
    "access_history": ("high", "Snowflake's access history"),
    "query_history": ("high", "Snowflake's query history"),
}


def classify_object(o: dict[str, Any], state: dict[str, Any], kind: str, now: datetime, days: int, guarded: list[str],
                    kept: dict[str, Any] | None) -> dict[str, Any]:
    """A view, procedure or function: unused only when the evidence for its kind covers the whole threshold."""
    seen_by = (state.get("object_usage") or {}).get({"view": "views", "procedure": "procedures", "function": "functions"}[o["type"]])
    last = o.get("last_used")
    if kind == "sqlserver" and o.get("code") in ("V", "IF", "TF"):
        seen_by = "base_tables" if state.get("usage") == "dmv" else None
        last, known = _through_tables(f"{o['schema']}.{o['name']}", state.get("objects") or {}, state.get("tables") or {})
        if not known:
            seen_by = None  # reads only tables in other databases (or none): can't be judged from here
    covered = state.get("covered_since") if seen_by != "query_history" else iso(now - timedelta(days=365))
    start = max((x for x in (covered, o.get("created")) if x), default=None) if covered else None
    seen_days = _days(now, start)
    idle_since = last or start
    idle = _days(now, idle_since)
    out: dict[str, Any] = {"seen_days": round(seen_days, 1) if seen_days is not None else None, "idle_days": round(idle, 1) if idle is not None else None,
                           "since": idle_since, "sure_in_days": None, "last_used": last, "evidence": seen_by}
    conf, source = EVIDENCE.get(seen_by or "", ("low", ""))
    verb = {"view": "read", "procedure": "run", "function": "called"}[o["type"]]
    if kept:
        out.update(status="kept", why=f"Kept by {kept.get('by')}" + (f": {kept['reason']}" if kept.get("reason") else ""))
    elif guarded:
        out.update(status="in_use", why="Used by " + ", ".join(guarded))
    elif seen_by and seen_days is not None:
        if last and idle is not None and idle < days:
            out.update(status="in_use", why=(f"Its tables were read {_ago(idle)} ago" if seen_by == "base_tables" else f"{verb.capitalize()} {_ago(idle)} ago"))
        elif seen_days >= days:
            why = (f"None of its tables were read for {_ago(idle)}, so it can't have been queried" if seen_by == "base_tables"
                   else f"Not {verb} for {_ago(idle)}" if last else f"Never {verb}")
            out.update(status="unused", confidence=conf, why=f"{why} (from {source}, watched for {_ago(seen_days)})")
        else:
            out.update(status="watching", sure_in_days=round(days - seen_days, 1),
                       why=f"Not {verb} in the {_ago(seen_days)} the bot can see; certain in {_ago(days - seen_days)}")
    else:
        out.update(status="unknown", why=("It reads tables in another database: its use can't be seen from here"
                                          if o["type"] == "view" and kind == "sqlserver" and state.get("usage") == "dmv"
                                          else f"Whether it's {verb} can't be seen with this login"))
    return out


def _ago(d: float | None) -> str:
    if d is None:
        return "?"
    if d < 1:
        return f"{max(1, round(d * 24))} h"
    if d < 60:
        return f"{round(d)} days"
    if d < 730:
        return f"{round(d / 30.4)} months"
    return f"{d / 365:.1f} years"


GRANTS = {
    "sqlserver": ("To see which tables, views and procedures are used, the bot's login needs to read SQL Server's usage statistics. "
                  "Run one of these as an admin:",
                  ["-- Azure SQL Database (in the database)\nGRANT VIEW DATABASE STATE TO [{user}];",
                   "-- SQL Server 2022 and later (in master)\nGRANT VIEW SERVER PERFORMANCE STATE TO [{user}];",
                   "-- SQL Server 2019 and earlier (in master)\nGRANT VIEW SERVER STATE TO [{user}];"]),
    "snowflake": ("To see reads and procedure calls, Snowflake's ACCESS_HISTORY and QUERY_HISTORY are needed: Enterprise edition, and the "
                  "bot's role allowed to read the SNOWFLAKE database:",
                  ["GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE {role};"]),
}


def _sum(rs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tables": len(rs), "count": len(rs), "gb": round(sum(r.get("gb") or 0 for r in rs), 3), "monthly": round(sum(r.get("monthly") or 0 for r in rs), 2)}


def database_rows(session: Session, conn: DbConnection, state: dict[str, Any], days: int, now: datetime) -> dict[str, Any]:
    price, price_note, custom = price_of(conn, state)
    guard = protected(session, conn)
    kept = state.get("keep") or {}
    rows = []
    for key, t in sorted((state.get("tables") or {}).items()):
        c = classify(t, state, conn.kind, now, days, guard.get(key.upper(), []), kept.get(key))
        # what the reads alone say (a table a pipeline loads is protected, but is anyone actually reading it?)
        evidence = classify(t, state, conn.kind, now, days, [], None)["status"] if c["status"] in ("in_use", "kept") and (guard.get(key.upper()) or kept.get(key)) else c["status"]
        gb = (t.get("bytes") or 0) / GB
        rows.append({**{k: t.get(k) for k in ("schema", "table", "rows", "bytes", "created", "last_read", "last_write", "altered")},
                     "kind": "table", "key": key, "gb": round(gb, 4), "monthly": round(gb * price, 4), "yearly": round(gb * price * 12, 2),
                     "deps": (state.get("deps") or {}).get(key, []), "read_status": evidence, **c})
    objects = state.get("objects") or {}
    objs = []
    for key, o in sorted(objects.items()):
        c = classify_object(o, state, conn.kind, now, days, guard.get(key.upper(), []), kept.get(key))
        objs.append({"kind": o["type"], "key": key, "schema": o["schema"], "table": o["name"], "created": o.get("created"),
                     "altered": o.get("altered"), "uses": o.get("uses") or [], "used_by": o.get("used_by") or [],
                     "deps": [{"object": u, "kind": objects[u]["type"] if u in objects else "object"} for u in o.get("used_by") or []],
                     "gb": 0.0, "monthly": 0.0, "yearly": 0.0, **c})
    # something an object in use calls is in use too (a live procedure reading a view keeps the view)
    by_key = {r["key"]: r for r in objs}
    for _ in range(4):
        changed = False
        for r in objs:
            if r["status"] in ("unused", "watching", "unknown"):
                users = [by_key[u] for u in r["used_by"] if u in by_key and by_key[u]["status"] in ("in_use", "kept")]
                if users:
                    r.update(status="in_use", why=f"Used by {users[0]['kind']} {users[0]['key']}, which is in use", sure_in_days=None)
                    changed = True
        if not changed:
            break
    hint = GRANTS.get(conn.kind) if state.get("usage") not in ("dmv", "access_history") and state.get("scanned_at") else None
    totals: dict[str, Any] = {g: _sum([r for r in rows if r["status"] == g]) for g in STATUSES} | {"all": _sum(rows)}
    by_kind = {k: {g: _sum([r for r in objs if r["kind"] == k and r["status"] == g]) for g in STATUSES}
               | {"all": _sum([r for r in objs if r["kind"] == k])} for k in ("view", "procedure", "function")}
    ou = state.get("object_usage") or {}
    return {
        "id": conn.id, "name": conn.name, "kind": conn.kind, "host": conn.host, "database": conn.database, "status": conn.status,
        "scanned_at": state.get("scanned_at"), "error": state.get("error"), "usage": state.get("usage"), "usage_error": state.get("usage_error"),
        "covered_since": state.get("covered_since"), "server_start": state.get("server_start"),
        "price": price, "price_note": price_note, "price_custom": custom,
        "grant": {"why": hint[0], "sql": [s.format(user=conn.username or "<login>", role=(conn.options or {}).get("role") or "<role>") for s in hint[1]]}
        if hint else None,
        "tables": rows, "totals": totals, "objects": objs, "object_totals": by_kind,
        "object_evidence": {"view": "base_tables" if conn.kind == "sqlserver" and state.get("usage") == "dmv" else ou.get("views"),
                            "procedure": ou.get("procedures"), "function": ou.get("functions")},
    }


def report(session: Session, user: User, days: int, now: datetime | None = None, conn: int | None = None) -> dict[str, Any]:
    """Every database of the person's, or just one (`conn`). Pipelines aren't inside one database, so they're always
    judged against all of them; with one database chosen, the savings headline counts that database only."""
    from .idle import pipelines_report

    now = now or utcnow()
    every = [database_rows(session, c, state_of(session, c.id), days, now)
             for c in session.scalars(select(DbConnection).where(DbConnection.user_id == user.id).order_by(DbConnection.name))]
    dbs = [d for d in every if conn is None or d["id"] == conn]
    all_unused = [r for d in dbs for r in d["tables"] if r["status"] == "unused"]
    objs = [r for d in dbs for r in d["objects"]]
    pipes = pipelines_report(session, user, days, every, now)
    pipe_monthly = pipes["summary"]["monthly"] if conn is None else 0.0
    return {"days": days, "generated_at": iso(now), "databases": dbs, "pipelines": pipes, "scope": conn,
            "all_databases": [{"id": d["id"], "name": d["name"], "kind": d["kind"]} for d in every],
            "summary": {"unused_tables": len(all_unused), "unused_gb": round(sum(r["gb"] for r in all_unused), 3),
                        "monthly": round(sum(r["monthly"] for r in all_unused), 2), "yearly": round(sum(r["yearly"] for r in all_unused), 2),
                        "watching": sum(d["totals"]["watching"]["tables"] for d in dbs),
                        "watching_monthly": round(sum(d["totals"]["watching"]["monthly"] for d in dbs), 2),
                        "write_only": sum(d["totals"]["write_only"]["tables"] for d in dbs),
                        "total_gb": round(sum(d["totals"]["all"]["gb"] for d in dbs), 3),
                        "total_monthly": round(sum(d["totals"]["all"]["monthly"] for d in dbs), 2),
                        "unused_views": sum(r["kind"] == "view" and r["status"] == "unused" for r in objs),
                        "unused_procedures": sum(r["kind"] == "procedure" and r["status"] == "unused" for r in objs),
                        "unused_functions": sum(r["kind"] == "function" and r["status"] == "unused" for r in objs),
                        "unused_pipelines": pipes["summary"]["unused"], "wasted_pipelines": pipes["summary"]["wasted"],
                        "pipelines_monthly": pipe_monthly,
                        "all_monthly": round(sum(r["monthly"] for r in all_unused) + pipe_monthly, 2),
                        "all_yearly": round((sum(r["monthly"] for r in all_unused) + pipe_monthly) * 12, 2)}}


# ── the script (the bot never runs it) ────────────────────────────────────────


def _ident(kind: str, name: str) -> str:
    name = safe_name(name)
    return f"[{name.replace(']', ']]')}]" if kind == "sqlserver" else f'"{name.replace(chr(34), chr(34) * 2)}"'


ORDER = {"procedure": 0, "function": 1, "view": 2, "table": 3}  # what calls things goes first; tables last


def script(conn: DbConnection, state: dict[str, Any], keys: list[str], mode: str, by: str, now: datetime | None = None,
           price: float = 0.0) -> dict[str, Any]:
    """A reviewed, commented script to remove (or archive) the chosen tables, views, procedures and functions. Only
    what the latest scan found; what someone kept is left out, with a note why."""
    now = now or utcnow()
    tables = state.get("tables") or {}
    objects = state.get("objects") or {}
    kept = state.get("keep") or {}
    chosen: list[dict[str, Any]] = []
    skipped, warnings = [], []
    picked = set(keys)
    for key in dict.fromkeys(keys):
        t = tables.get(key)
        o = objects.get(key)
        if t is None and o is None:
            skipped.append(f"{key}: not in the latest scan")
            continue
        if key in kept:
            skipped.append(f"{key}: kept by {kept[key].get('by')}")
            continue
        item = {**t, "kind": "table", "name": t["table"]} if t is not None else {**o, "kind": o["type"]}  # type: ignore[dict-item]
        if _names(item.get("schema"), item.get("name")) is None:
            skipped.append(f"{key}: its name can't be written into a script safely; remove it by hand")
            continue
        if conn.kind == "snowflake" and item["kind"] in ("procedure", "function") and not item.get("signatures"):
            skipped.append(f"{key}: its argument types couldn't be read, so DROP can't name it exactly; remove it by hand")
            continue
        chosen.append(item)
        if item["kind"] == "table":
            for d in (state.get("deps") or {}).get(key, []):
                if d["object"] not in picked:
                    warnings.append(f"{key} is referenced by {d['kind']} {d['object']}: "
                                    f"{'the drop fails until it is removed' if d['kind'] == 'foreign key' else 'it will stop working'}")
        else:
            for u in item.get("used_by") or []:
                if u not in picked:
                    what = objects[u]["type"] if u in objects else "object"
                    warnings.append(f"{item['kind'].capitalize()} {key} is used by {what} {u}: it will stop working")
    chosen.sort(key=lambda x: ORDER[x["kind"]])
    n_tables = sum(x["kind"] == "table" for x in chosen)
    gb = sum((x.get("bytes") or 0) for x in chosen if x["kind"] == "table") / GB
    k = conn.kind
    q = lambda *parts: ".".join(_ident(k, p) for p in parts)  # noqa: E731
    count = _count_text(chosen)
    head = [f"Generated by AlertBot on {now:%Y-%m-%d %H:%M} UTC for {by}",
            f"Database: {conn.name} ({conn.database}{' on ' + conn.host if conn.host else ''})",
            f"{count} · {gb:.2f} GB of tables · about ${gb * price:,.2f} a month, ${gb * price * 12:,.2f} a year",
            "AlertBot never runs this. Review it, then run it yourself with a login that's allowed to change the database."]
    if mode == "archive":
        head.append("ARCHIVE: moves each object into an [archive] schema, so nothing is lost yet. Drop the archive schema's objects later.")
    if state.get("usage") not in ("dmv", "access_history") and n_tables:
        warnings.append("Reads can't be seen on this database with the bot's login: these tables haven't changed for a long time, "
                        "but check nobody still queries them before running this")
    if any(x["kind"] in ("procedure", "function") for x in chosen) and k == "sqlserver":
        warnings.append("Procedures and functions are judged from SQL Server's execution statistics, which it can forget when memory "
                        "is short: check a scheduled job, report or application doesn't call them before running this")
    if k == "snowflake" and mode != "archive" and n_tables:
        head.append("Snowflake keeps dropped tables for their Time Travel retention (UNDROP TABLE restores them), then 7 days of Fail-safe: "
                    "storage savings start after that.")
    elif k == "sqlserver" and n_tables:
        head.append("Dropping frees space inside the database. On Azure SQL the bill follows the database's max size: lower it after, to save.")
    if any(x["kind"] != "table" for x in chosen):
        head.append("Views, procedures and functions take no storage; removing them tidies the database. Keep a copy of each "
                    "definition first (SSMS: Script As > CREATE; Snowflake: SELECT GET_DDL(...)).")
    wrap = lambda text, lead: [lead + x for x in textwrap.wrap(text, 88, break_long_words=False, break_on_hyphens=False)]  # noqa: E731
    lines = [x for h in head for x in wrap(h, "-- ")]
    if warnings:
        lines += ["--", "-- REVIEW FIRST:"] + [x for w in warnings for x in wrap(w, "--   ")]
    if skipped:
        lines += ["--", "-- Left out:"] + [x for w in skipped for x in wrap(w, "--   ")]
    lines.append("")
    if k == "sqlserver":
        if mode == "archive":
            lines.append("IF SCHEMA_ID(N'archive') IS NULL EXEC(N'CREATE SCHEMA [archive]');")
        lines.append("BEGIN TRANSACTION;")
        for x in chosen:
            full = q(x["schema"], x["name"])
            lit = f"{x['schema']}.{x['name']}".replace("'", "''")
            if x["kind"] == "table":
                code, keyword, note = "U", "TABLE", f"{(x.get('bytes') or 0) / GB:.3f} GB, {int(x.get('rows') or 0):,} rows"
            else:
                _, code, keyword = SQL_TYPES[x["code"]]
                note = x["kind"]
            if mode == "archive":
                lines.append(f"IF OBJECT_ID(N'{lit}', N'{code}') IS NOT NULL ALTER SCHEMA [archive] TRANSFER {full};  -- {note}")
            else:
                lines.append(f"IF OBJECT_ID(N'{lit}', N'{code}') IS NOT NULL DROP {keyword} {full};  -- {note}")
        lines += ["COMMIT TRANSACTION;", "-- If anything above fails, nothing is changed: run ROLLBACK TRANSACTION; and check the error."]
    elif k == "snowflake":
        db = conn.database
        if mode == "archive":
            lines.append(f"CREATE SCHEMA IF NOT EXISTS {q(db, 'ARCHIVE')};")
        for x in chosen:
            full = q(db, x["schema"], x["name"])
            if x["kind"] == "table":
                size = f"{(x.get('bytes') or 0) / GB:.3f} GB, {int(x.get('rows') or 0):,} rows"
                lines.append(f"ALTER TABLE IF EXISTS {full} RENAME TO {q(db, 'ARCHIVE', x['name'])};  -- {size}" if mode == "archive"
                             else f"DROP TABLE IF EXISTS {full};  -- {size}")
            elif x["kind"] == "view":
                lines.append(f"ALTER VIEW IF EXISTS {full} RENAME TO {q(db, 'ARCHIVE', x['name'])};" if mode == "archive"
                             else f"DROP VIEW IF EXISTS {full};")
            else:
                word = x["kind"].upper()
                for sig in x["signatures"]:
                    lines.append(f"ALTER {word} IF EXISTS {full}({sig}) RENAME TO {q(db, 'ARCHIVE', x['name'])};" if mode == "archive"
                                 else f"DROP {word} IF EXISTS {full}({sig});")
        if mode != "archive" and n_tables:
            lines += ["", "-- To undo within the Time Travel retention:"] + [f"-- UNDROP TABLE {q(db, x['schema'], x['name'])};"
                                                                              for x in chosen if x["kind"] == "table"]
    else:
        for x in chosen:
            lines.append(f"DROP {'TABLE' if x['kind'] == 'table' else 'VIEW'} IF EXISTS {_ident(k, x['name'])};")
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{conn.name}-{'archive' if mode == 'archive' else 'drop'}-{len(chosen)}-objects-{now:%Y%m%d}").strip("-")
    return {"sql": "\n".join(lines) + "\n", "filename": f"{name}.sql", "tables": n_tables, "items": len(chosen),
            "objects": len(chosen) - n_tables, "gb": round(gb, 3),
            "monthly": round(gb * price, 2), "yearly": round(gb * price * 12, 2), "warnings": warnings, "skipped": skipped, "mode": mode}


def _count_text(chosen: list[dict[str, Any]]) -> str:
    """"2 tables, 1 view and 3 procedures"."""
    names = {"table": ("table", "tables"), "view": ("view", "views"), "procedure": ("procedure", "procedures"), "function": ("function", "functions")}
    parts = [f"{n} {names[k][n != 1]}" for k in ORDER if (n := sum(x["kind"] == k for x in chosen))]
    if not parts:
        return "nothing"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def scan_all(now: datetime | None = None) -> int:
    """Worker: scan every connected database that's due (hourly), so usage is recorded continuously."""
    from ..db import session_scope

    now = now or utcnow()
    with session_scope() as s:
        ids = [c.id for c in s.scalars(select(DbConnection))]
    n = 0
    for cid in ids:
        with session_scope() as s:
            conn = s.get(DbConnection, cid)
            st = state_of(s, cid)
            if conn is None or (st.get("scanned_at") and now - _dt(st["scanned_at"]) < SCAN_EVERY):
                continue
        with session_scope() as s:
            conn = s.get(DbConnection, cid)
            if conn is not None:
                scan(s, conn, now)
                n += 1
    return n
