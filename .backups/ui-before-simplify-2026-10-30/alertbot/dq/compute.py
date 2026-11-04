"""Snowflake compute cost: what the warehouses (and Snowflake's serverless features) cost, who and what spends it, what's
wasted, and what to change, with alerts when it jumps.

How Snowflake bills compute: credits. A running warehouse burns credits every second it's on, at its size's rate
(X-Small 1 credit an hour, doubling each size), even with nothing to do until it suspends. Tasks, Snowpipe, automatic
clustering and a few others burn credits without a warehouse. A credit costs a contract price ($2-4 on the list).

Where the numbers come from (SNOWFLAKE.ACCOUNT_USAGE, up to ~3 h behind; INFORMATION_SCHEMA for what's running now):
    METERING_DAILY_HISTORY       credits billed per day per service (the bill)
    WAREHOUSE_METERING_HISTORY   credits per warehouse per hour (what each warehouse burned, idle time included)
    QUERY_ATTRIBUTION_HISTORY    credits each query used (no idle time): by user, query tag, repeated query shape
    QUERY_HISTORY                queueing, run times, roles (never the query text)
    STORAGE_USAGE                bytes stored per day (storage + compute = the Snowflake bill)
    SHOW WAREHOUSES              sizes, auto-suspend, resource monitors (settings)
    ORGANIZATION_USAGE.RATE_SHEET_DAILY   the contract's price per credit, when the role may read it
Only totals, names and ids leave Snowflake: never a query's text, never a row of data. The bot's own queries are
tagged "alertbot-…", so its own cost is shown too. The bot never changes anything: it writes the script.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import BotState, DbConnection, Project, Run, User, UserProject
from ..timeutil import iso, utcnow
from . import sql as T
from .client import DbError
from .privacy import RawDataRejected, safe_name
from .storage import _dt, _num, _utc

log = logging.getLogger(__name__)

KEY = "compute:conn:{}"
SCAN_EVERY = timedelta(hours=1)
RUNNING_EVERY = timedelta(minutes=10)
LIST_PRICE = 3.0  # $ per credit: Snowflake's on-demand list price for Enterprise (Standard $2, Business Critical $4; US regions)
STORAGE_TB = 23.0  # $ per TB-month on a capacity contract (on-demand is about $40)
SIZES = ["X-Small", "Small", "Medium", "Large", "X-Large", "2X-Large", "3X-Large", "4X-Large", "5X-Large", "6X-Large"]
CREDITS_PER_HOUR = {s: 2 ** i for i, s in enumerate(SIZES)}
SERVICE_NAMES = {"WAREHOUSE_METERING": "Warehouses", "WAREHOUSE_METERING_READER": "Reader warehouses", "AUTO_CLUSTERING": "Automatic clustering",
                 "PIPE": "Snowpipe", "SNOWPIPE_STREAMING": "Snowpipe Streaming", "SERVERLESS_TASK": "Serverless tasks",
                 "MATERIALIZED_VIEW": "Materialized views", "SEARCH_OPTIMIZATION": "Search optimization", "QUERY_ACCELERATION": "Query acceleration",
                 "REPLICATION": "Replication", "AI_SERVICES": "AI functions (Cortex)", "SERVERLESS_ALERTS": "Alerts", "CLOUD_SERVICES": "Cloud services",
                 "COPY_FILES": "Copy files", "TRUST_CENTER": "Trust Center", "SNOWPARK_CONTAINER_SERVICES": "Containers"}
PIPELINE_USER = re.compile(r"adf|svc|service|etl|pipeline|integration|loader|azure|synapse|airflow|dbt|fivetran|ingest", re.I)
BI_USER = re.compile(r"tableau|power.?bi|pbi|looker|sigma|qlik|metabase|superset|mode|thoughtspot|excel", re.I)  # reading tools, not pipelines
AU = "SNOWFLAKE.ACCOUNT_USAGE"
DEFAULTS: dict[str, Any] = {"price": None, "storage_tb": None, "budget": None, "warehouse_budgets": {}, "runaway_minutes": 30, "spike_factor": 3.0,
                            "pipeline_users": None, "alerts_on": True}


def _q(name: str, sql: str, params: dict[str, Any] | None = None) -> T.MetaQuery:
    return T.MetaQuery(name, sql, params or {})


def queries(days: int = 60) -> dict[str, T.MetaQuery]:
    ago = lambda d: f"DATEADD(day, -{int(d)}, CURRENT_TIMESTAMP())"  # noqa: E731
    return {
        "daily": _q("compute_daily", f"SELECT USAGE_DATE, SERVICE_TYPE, SUM(CREDITS_USED), SUM(CREDITS_BILLED) FROM {AU}.METERING_DAILY_HISTORY "
                                     f"WHERE USAGE_DATE >= DATEADD(day, -{days}, CURRENT_DATE()) GROUP BY 1, 2"),
        "hourly": _q("compute_hourly", f"SELECT WAREHOUSE_NAME, START_TIME, CREDITS_USED, CREDITS_USED_COMPUTE FROM {AU}.WAREHOUSE_METERING_HISTORY "
                                       f"WHERE START_TIME >= {ago(15)}"),
        "wh_daily": _q("compute_wh_daily", f"SELECT WAREHOUSE_NAME, TO_DATE(START_TIME), SUM(CREDITS_USED), SUM(CREDITS_USED_COMPUTE) "
                                           f"FROM {AU}.WAREHOUSE_METERING_HISTORY WHERE START_TIME >= {ago(days)} GROUP BY 1, 2"),
        "attributed": _q("compute_attributed", f"SELECT WAREHOUSE_NAME, TO_DATE(START_TIME), SUM(CREDITS_ATTRIBUTED_COMPUTE), COUNT(*) "
                                               f"FROM {AU}.QUERY_ATTRIBUTION_HISTORY WHERE START_TIME >= {ago(30)} GROUP BY 1, 2"),
        "users": _q("compute_users", f"SELECT USER_NAME, TO_DATE(START_TIME), SUM(CREDITS_ATTRIBUTED_COMPUTE), COUNT(*) "
                                     f"FROM {AU}.QUERY_ATTRIBUTION_HISTORY WHERE START_TIME >= {ago(30)} GROUP BY 1, 2"),
        "tags": _q("compute_tags", f"SELECT QUERY_TAG, SUM(CREDITS_ATTRIBUTED_COMPUTE), COUNT(*) FROM {AU}.QUERY_ATTRIBUTION_HISTORY "
                                   f"WHERE START_TIME >= {ago(30)} GROUP BY 1 ORDER BY 2 DESC LIMIT 60"),
        "roles": _q("compute_roles", f"SELECT h.ROLE_NAME, SUM(a.CREDITS_ATTRIBUTED_COMPUTE), COUNT(*) FROM {AU}.QUERY_ATTRIBUTION_HISTORY a "
                                     f"JOIN {AU}.QUERY_HISTORY h ON h.QUERY_ID = a.QUERY_ID WHERE a.START_TIME >= {ago(30)} "
                                     f"AND h.START_TIME >= {ago(31)} GROUP BY 1 ORDER BY 2 DESC LIMIT 60"),
        # the costliest repeated query shapes: their fingerprint, never their text
        "shapes": _q("compute_shapes", f"SELECT a.QUERY_PARAMETERIZED_HASH, SUM(a.CREDITS_ATTRIBUTED_COMPUTE), COUNT(*), MAX(a.START_TIME), "
                                       f"MAX_BY(a.QUERY_ID, a.START_TIME), MODE(a.WAREHOUSE_NAME), MODE(a.USER_NAME), MODE(h.QUERY_TYPE), "
                                       f"MODE(h.DATABASE_NAME), MODE(h.SCHEMA_NAME), AVG(h.TOTAL_ELAPSED_TIME), AVG(h.BYTES_SCANNED), MODE(a.QUERY_TAG) "
                                       f"FROM {AU}.QUERY_ATTRIBUTION_HISTORY a JOIN {AU}.QUERY_HISTORY h ON h.QUERY_ID = a.QUERY_ID "
                                       f"WHERE a.START_TIME >= {ago(30)} AND h.START_TIME >= {ago(31)} AND a.QUERY_PARAMETERIZED_HASH IS NOT NULL "
                                       f"GROUP BY 1 ORDER BY 2 DESC LIMIT 25"),
        # how each warehouse's queries behave: is it too big, too small?
        "wh_stats": _q("compute_wh_stats", f"SELECT WAREHOUSE_NAME, MAX_BY(WAREHOUSE_SIZE, START_TIME), COUNT(*), APPROX_PERCENTILE(EXECUTION_TIME, 0.9), "
                                           f"SUM(CASE WHEN QUEUED_OVERLOAD_TIME > 0 THEN 1 ELSE 0 END), AVG(QUEUED_OVERLOAD_TIME), "
                                           f"SUM(BYTES_SPILLED_TO_REMOTE_STORAGE), SUM(BYTES_SPILLED_TO_LOCAL_STORAGE), AVG(BYTES_SCANNED) "
                                           f"FROM {AU}.QUERY_HISTORY WHERE START_TIME >= {ago(14)} AND WAREHOUSE_NAME IS NOT NULL "
                                           f"AND EXECUTION_STATUS = 'SUCCESS' GROUP BY 1"),
        "storage": _q("compute_storage", f"SELECT USAGE_DATE, STORAGE_BYTES + STAGE_BYTES + FAILSAFE_BYTES FROM {AU}.STORAGE_USAGE "
                                         f"WHERE USAGE_DATE >= DATEADD(day, -{days}, CURRENT_DATE())"),
        "rate": _q("compute_rate", "SELECT MAX_BY(EFFECTIVE_RATE, USAGE_DATE), MAX_BY(CURRENCY, USAGE_DATE) FROM SNOWFLAKE.ORGANIZATION_USAGE.RATE_SHEET_DAILY "
                                   "WHERE USAGE_TYPE = 'compute' AND USAGE_DATE >= DATEADD(day, -40, CURRENT_DATE())"),
        "running": _q("compute_running", "SELECT QUERY_ID, USER_NAME, WAREHOUSE_NAME, WAREHOUSE_SIZE, START_TIME, "
                                         "DATEDIFF(second, START_TIME, CURRENT_TIMESTAMP()) FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => 2000)) "
                                         "WHERE EXECUTION_STATUS IN ('RUNNING', 'QUEUED')"),
    }


def buckets_query(users: list[str]) -> T.MetaQuery:
    """Five-minute buckets of the pipelines' logins' credits (and query tags): enough to match them to pipeline runs."""
    names = [f"u{i}" for i in range(len(users))]
    return _q("compute_pipeline_buckets",
              f"SELECT TIME_SLICE(START_TIME, 5, 'MINUTE', 'START'), USER_NAME, QUERY_TAG, SUM(CREDITS_ATTRIBUTED_COMPUTE), COUNT(*) "
              f"FROM {AU}.QUERY_ATTRIBUTION_HISTORY WHERE START_TIME >= DATEADD(day, -30, CURRENT_TIMESTAMP()) "
              f"AND USER_NAME IN ({', '.join(':' + n for n in names)}) GROUP BY 1, 2, 3", dict(zip(names, users, strict=True)))


# ── reading ───────────────────────────────────────────────────────────────────


def _name(v: Any) -> str | None:
    try:
        return safe_name(str(v)) if v is not None and str(v).strip() else None
    except RawDataRejected:
        return None


def _tag(v: Any) -> str:
    """A query tag as a short plain label (tags are labels apps set, but they could be anything: cut and cleaned)."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v or "")).strip()[:80]


def _day(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        v = _utc(v).date()  # type: ignore[union-attr]
    return v.isoformat() if isinstance(v, date) else str(v)[:10]


def read(client: Any, conn: DbConnection, pipeline_users: list[str] | None = None) -> dict[str, Any]:
    """Everything the account says about its compute (each part optional: what the role can't read is noted)."""
    q = queries()
    out: dict[str, Any] = {"errors": {}}

    def run(key: str, limit: int = 20000) -> list[Any] | None:
        try:
            return list(client.meta(q[key], limit))
        except DbError as exc:
            out["errors"][key] = str(exc)[:200]
            return None

    rows = run("daily")
    if rows is None:  # the bill itself is the one thing that must be readable
        raise DbError(_safe("no_usage", "This login can't read SNOWFLAKE.ACCOUNT_USAGE. Grant its role IMPORTED PRIVILEGES on the SNOWFLAKE database."))
    out["daily"] = [{"date": _day(d), "service": str(s or "OTHER"), "credits": _num(c) or 0.0, "billed": _num(b) if b is not None else _num(c) or 0.0}
                    for d, s, c, b in rows]
    out["hourly"] = [{"wh": w, "at": iso(_utc(t)), "credits": _num(c) or 0.0, "compute": _num(cc) or 0.0}
                     for wh, t, c, cc in run("hourly", 60000) or [] if (w := _name(wh))]
    out["wh_daily"] = [{"wh": w, "date": _day(d), "credits": _num(c) or 0.0, "compute": _num(cc) or 0.0}
                       for wh, d, c, cc in run("wh_daily", 60000) or [] if (w := _name(wh))]
    out["attributed"] = [{"wh": w, "date": _day(d), "credits": _num(c) or 0.0, "queries": int(_num(n) or 0)}
                         for wh, d, c, n in run("attributed", 60000) or [] if (w := _name(wh))]
    out["users"] = [{"user": u, "date": _day(d), "credits": _num(c) or 0.0, "queries": int(_num(n) or 0)}
                    for us, d, c, n in run("users", 60000) or [] if (u := _name(us))]
    out["tags"] = [{"tag": _tag(t), "credits": _num(c) or 0.0, "queries": int(_num(n) or 0)} for t, c, n in run("tags") or []]
    out["roles"] = [{"role": r, "credits": _num(c) or 0.0, "queries": int(_num(n) or 0)} for ro, c, n in run("roles") or [] if (r := _name(ro))]
    out["shapes"] = [{"hash": str(h)[:64], "credits": _num(c) or 0.0, "runs": int(_num(n) or 0), "last_at": iso(_utc(at)), "query_id": str(qid or "")[:64],
                      "wh": _name(wh), "user": _name(us), "type": str(qt or "")[:40], "database": _name(db), "schema": _name(sc),
                      "avg_ms": _num(ms), "avg_bytes": _num(by), "tag": _tag(tag)}
                     for h, c, n, at, qid, wh, us, qt, db, sc, ms, by, tag in run("shapes") or []]
    out["wh_stats"] = {w: {"size": str(size or ""), "queries": int(_num(n) or 0), "p90_ms": _num(p90), "queued": int(_num(qd) or 0), "avg_queue_ms": _num(aq),
                           "spill_remote": _num(sr) or 0.0, "spill_local": _num(sl) or 0.0, "avg_bytes": _num(by)}
                       for wh, size, n, p90, qd, aq, sr, sl, by in run("wh_stats") or [] if (w := _name(wh))}
    out["storage"] = [{"date": _day(d), "bytes": _num(b) or 0.0} for d, b in run("storage") or []]
    rate = run("rate")
    out["rate"] = {"price": _num(rate[0][0]), "currency": str(rate[0][1] or "USD")} if rate and rate[0][0] is not None else None
    out["warehouses"] = _warehouses(client, out)
    out["running"] = running(client, out)
    users = pipeline_users if pipeline_users is not None else guess_pipeline_users(out["users"])
    out["pipeline_users"] = users
    out["buckets"] = []
    if users:
        try:
            out["buckets"] = [{"at": iso(_utc(b)), "user": _name(u), "tag": _tag(t), "credits": _num(c) or 0.0, "queries": int(_num(n) or 0)}
                              for b, u, t, c, n in client.meta(buckets_query(users[:20]), 60000)]
        except DbError as exc:
            out["errors"]["buckets"] = str(exc)[:200]
    return out


def _safe(code: str, msg: str) -> Any:
    from .privacy import SafeError

    return SafeError(code, msg)


def _warehouses(client: Any, out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    try:
        keys, rows = client.warehouses()
    except DbError as exc:
        out["errors"]["warehouses"] = str(exc)[:200]
        return {}
    k = [str(x).lower() for x in keys]
    got = {}
    for row in rows:
        r = dict(zip(k, row, strict=False))
        if not (name := _name(r.get("name"))):
            continue
        susp = r.get("auto_suspend")
        got[name] = {"size": str(r.get("size") or ""), "state": str(r.get("state") or ""), "type": str(r.get("type") or "STANDARD"),
                     "min_clusters": int(_num(r.get("min_cluster_count")) or 1), "max_clusters": int(_num(r.get("max_cluster_count")) or 1),
                     "auto_suspend": None if susp in (None, "", "null") else int(_num(susp) or 0), "auto_resume": str(r.get("auto_resume")).lower() == "true",
                     "resource_monitor": None if str(r.get("resource_monitor") or "null").lower() == "null" else _name(r.get("resource_monitor"))}
    return got


def running(client: Any, out: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    try:
        rows = client.meta(queries()["running"], 2000)
    except DbError as exc:
        if out is not None:
            out["errors"]["running"] = str(exc)[:200]
        return []
    return [{"id": str(i)[:64], "user": _name(u), "wh": _name(w), "size": str(sz or ""), "start": iso(_utc(st)), "seconds": int(_num(sec) or 0)}
            for i, u, w, sz, st, sec in rows]


def guess_pipeline_users(users: list[dict[str, Any]]) -> list[str]:
    """Logins that look like a service the pipelines sign in as (a person can change the list)."""
    return sorted({u["user"] for u in users if PIPELINE_USER.search(u["user"]) and not BI_USER.search(u["user"])})[:20]


# ── state ─────────────────────────────────────────────────────────────────────


def state_of(session: Session, conn_id: int) -> dict[str, Any]:
    row = session.get(BotState, KEY.format(conn_id))
    return dict(row.value) if row and row.value else {}


def save_state(session: Session, conn_id: int, state: dict[str, Any]) -> None:
    row = session.get(BotState, KEY.format(conn_id)) or BotState(key=KEY.format(conn_id), value={})
    row.value, row.updated_at = state, utcnow()
    session.add(row)
    session.flush()


def settings_of(state: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **(state.get("settings") or {})}


def price_of(state: dict[str, Any]) -> tuple[float, str, str]:
    """(price per credit, where it comes from: yours | contract | list, a note)."""
    s = settings_of(state)
    if s["price"] is not None:
        return float(s["price"]), "yours", "set by you"
    rate = (state.get("read") or {}).get("rate")
    if rate and rate.get("price"):
        return float(rate["price"]), "contract", f"your contract's rate, read from Snowflake ({rate.get('currency') or 'USD'})"
    return LIST_PRICE, "list", "Snowflake's list price for Enterprise edition (Standard is $2, Business Critical $4)"


# ── the work: scan (hourly), running queries (every 10 minutes) ──────────────


def scan(session: Session, conn: DbConnection, now: datetime | None = None) -> dict[str, Any]:
    """Read the account (nothing written meanwhile), link credits to pipeline runs, look for alerts, and keep it."""
    from .service import client_of

    now = now or utcnow()
    prev = state_of(session, conn.id)
    s = settings_of(prev)
    try:
        got = read(client_of(conn), conn, s["pipeline_users"])
        error = None
    except (DbError, Exception) as exc:  # noqa: BLE001 - shown on the page; the last good read stays
        got, error = None, str(exc)[:300] if isinstance(exc, DbError) else "Snowflake couldn't be read just now"
    if got is None:
        state = {**prev, "error": error, "error_at": iso(now)}
    else:
        state = {**prev, "read": {k: v for k, v in got.items() if k != "buckets"}, "scanned_at": iso(now), "running_at": iso(now), "error": None}
        state["pipelines"] = attribute(session, conn, got.get("buckets") or [], now)
        state = detect(state, now)
    save_state(session, conn.id, state)
    return state


def watch_running(session: Session, conn: DbConnection, now: datetime) -> dict[str, Any]:
    from .service import client_of

    state = state_of(session, conn.id)
    try:
        rows = running(client_of(conn))
    except Exception:  # noqa: BLE001 - next time
        return state
    state = {**state, "read": {**(state.get("read") or {}), "running": rows}, "running_at": iso(now)}
    state = detect(state, now, only_running=True)
    save_state(session, conn.id, state)
    return state


def scan_all(now: datetime | None = None) -> int:
    """Worker: each connected Snowflake account, hourly (what's running: every 10 minutes); then send new alerts."""
    from ..db import session_scope

    now = now or utcnow()
    with session_scope() as s:
        ids = [c.id for c in s.scalars(select(DbConnection).where(DbConnection.kind == "snowflake"))]
    n = 0
    for cid in ids:
        with session_scope() as s:
            st = state_of(s, cid)
        full = not st.get("scanned_at") or now - _dt(st["scanned_at"]) >= SCAN_EVERY
        quick = not st.get("running_at") or now - _dt(st["running_at"]) >= RUNNING_EVERY
        if not full and not quick:
            continue
        with session_scope() as s:
            conn = s.get(DbConnection, cid)
            if conn is None:
                continue
            if full:
                scan(s, conn, now)
            else:
                watch_running(s, conn, now)
        deliver(cid, now)
        n += 1
    return n


# ── pipelines: whose runs used these credits ─────────────────────────────────


def attribute(session: Session, conn: DbConnection, buckets: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Credits of the pipelines' logins, given to pipeline runs: by query tag when a tag names the pipeline, otherwise by
    time (a five-minute bucket during a run, shared between runs that overlap). What matches no run is "unlinked"."""
    from ..engine.adf import inventory_of

    since = now - timedelta(days=30)
    projects = list(session.scalars(select(Project).join(UserProject, UserProject.project_id == Project.id)
                                    .where(UserProject.user_id == conn.user_id, Project.active.is_(True))))
    touching: dict[tuple[str, str], str] = {}  # (project key, pipeline) → project name; pipelines that read or write Snowflake
    for p in projects:
        for it in inventory_of(session, p.key).get("items") or []:
            hits = [t for st in it.get("lineage") or [] for t in (st.get("reads") or []) + (st.get("writes") or []) if t.get("platform") == "Snowflake"]
            if hits:
                touching[(p.key, it["name"])] = p.display_name
    runs: list[tuple[str, str, datetime, datetime]] = []
    for p in projects:
        for r in session.scalars(select(Run).where(Run.project_id == p.id, Run.requested_at >= since)):
            if (p.key, r.site) in touching and r.started_at:
                end = r.ended_at or (r.started_at + timedelta(seconds=r.duration_s or 300))
                runs.append((p.key, r.site, r.started_at, end))
    per: dict[tuple[str, str], dict[str, Any]] = {k: {"credits": 0.0, "queries": 0, "runs": 0, "by_tag": 0.0} for k in touching}
    for key, site, *_ in runs:
        per[(key, site)]["runs"] += 1
    unlinked = 0.0
    names = {k[1].lower(): k for k in touching}
    for b in buckets:
        c = b["credits"]
        tagged = next((k for n, k in names.items() if b["tag"] and n in b["tag"].lower()), None)
        if tagged:
            per[tagged]["credits"] += c
            per[tagged]["by_tag"] += c
            per[tagged]["queries"] += b["queries"]
            continue
        at = _dt(b["at"])
        hit = [(k, s) for k, s, a, e in runs if a - timedelta(minutes=5) <= at <= e + timedelta(minutes=5)]
        if not hit:
            unlinked += c
            continue
        for k, s in hit:
            per[(k, s)]["credits"] += c / len(hit)
            per[(k, s)]["queries"] += b["queries"] / len(hit)
    rows = [{"project": k, "project_name": touching[(k, s)], "pipeline": s, "credits": round(v["credits"], 4), "runs": v["runs"],
             "per_run": round(v["credits"] / v["runs"], 4) if v["runs"] else None, "queries": int(v["queries"]),
             "how": "tag" if v["by_tag"] and v["by_tag"] >= v["credits"] * 0.5 else "time"} for (k, s), v in per.items()]
    rows.sort(key=lambda r: -r["credits"])
    return {"rows": rows, "unlinked": round(unlinked, 4), "linked_pipelines": len(touching), "runs": len(runs)}


# ── alerts: spikes, runaway queries, budget ───────────────────────────────────


def _month(d: date) -> str:
    return f"{d:%Y-%m}"


def spend(state: dict[str, Any], today: date) -> dict[str, Any]:
    """Credits billed: yesterday, the usual day, this month so far, last month, and where this month is heading."""
    daily: dict[str, float] = defaultdict(float)
    for r in (state.get("read") or {}).get("daily") or []:
        daily[r["date"]] += r["billed"]
    yday = (today - timedelta(days=1)).isoformat()
    usual = [daily.get((today - timedelta(days=i)).isoformat(), 0.0) for i in range(2, 9)]
    mtd = sum(v for d, v in daily.items() if d.startswith(_month(today)))
    first = today.replace(day=1)
    last_month = sum(v for d, v in daily.items() if d.startswith(_month(first - timedelta(days=1))))
    next_month = (first + timedelta(days=32)).replace(day=1)
    days_in, done = (next_month - first).days, today.day - 1  # today isn't complete
    recent = [daily.get((today - timedelta(days=i)).isoformat(), 0.0) for i in range(1, 8)]
    pace = statistics.fmean(recent) if any(recent) else (mtd / done if done else 0.0)
    forecast = mtd + pace * (days_in - done)
    return {"daily": dict(daily), "yesterday": daily.get(yday, 0.0), "usual_day": statistics.median(usual) if any(usual) else None,
            "mtd": mtd, "last_month": last_month, "forecast": forecast, "days_in_month": days_in, "pace": pace}


def detect(state: dict[str, Any], now: datetime, only_running: bool = False) -> dict[str, Any]:
    s = settings_of(state)
    rd = state.get("read") or {}
    price = price_of(state)[0]
    alerts = list(state.get("alerts") or [])
    seen = {a["key"] for a in alerts}
    new: list[dict[str, Any]] = []

    def add(key: str, kind: str, severity: str, title: str, detail: str, wh: str | None = None) -> None:
        if key not in seen:
            new.append({"key": key, "kind": kind, "severity": severity, "title": title, "detail": detail, "wh": wh, "at": iso(now), "delivery": None})
            seen.add(key)

    for q in rd.get("running") or []:
        mins = q["seconds"] / 60
        if mins >= s["runaway_minutes"]:
            size = q["size"] or "a"
            add(f"runaway:{q['id']}", "runaway", "warning", f"A query has been running {int(mins)} minutes on {q['wh']}",
                f"{q['user'] or 'Someone'}'s query {q['id']} on {q['wh']} ({size} warehouse, about {2 ** (size_index(q['size']) or 0)} credits an hour while it runs). "
                "Check whether it's stuck, and cancel it in Snowsight if it is.", q["wh"])
    if not only_running:
        hourly = rd.get("hourly") or []
        by_wh: dict[str, dict[str, float]] = defaultdict(dict)
        for h in hourly:
            by_wh[h["wh"]][h["at"]] = h["credits"]
        for wh, series in by_wh.items():
            if not series:
                continue
            last = max(series)
            c = series[last]
            hour = _dt(last).hour
            base = [v for at, v in series.items() if at != last and _dt(at).hour == hour and _dt(last) - _dt(at) <= timedelta(days=14)]
            if len(base) >= 5:
                med = statistics.median(base)
                if c >= 1 and c > s["spike_factor"] * max(med, 0.25):
                    add(f"spike:{wh}:{last}", "spike", "warning", f"{wh} used {c / max(med, 0.25):.0f}× its usual credits",
                        f"{c:.1f} credits (about ${c * price:,.2f}) in the hour from {_dt(last):%H:%M} UTC; that hour usually takes {med:.1f}. "
                        "Look at who and what ran then, on the Compute cost page.", wh)
        today = now.date()
        sp = spend(state, today)
        budget = s["budget"]
        if budget:
            mtd, fc = sp["mtd"] * price, sp["forecast"] * price
            for pct in (100, 80):
                if mtd >= budget * pct / 100:
                    add(f"budget:{pct}:{_month(today)}", "budget", "critical" if pct == 100 else "warning",
                        f"Snowflake compute is at {mtd / budget:.0%} of this month's budget",
                        f"${mtd:,.0f} of ${budget:,.0f} spent so far this month; heading for about ${fc:,.0f}.")
                    break
            else:
                if fc > budget * 1.05 and today.day >= 5:
                    add(f"budget:forecast:{_month(today)}", "budget", "warning", "Snowflake compute is heading over budget",
                        f"About ${fc:,.0f} by the end of the month at this pace, against a budget of ${budget:,.0f}.")
        whb = s.get("warehouse_budgets") or {}
        if whb:
            wmtd: dict[str, float] = defaultdict(float)
            for r in rd.get("wh_daily") or []:
                if r["date"].startswith(_month(today)):
                    wmtd[r["wh"]] += r["credits"]
            for wh, b in whb.items():
                if b and wmtd.get(wh, 0) * price >= b * 0.8:
                    pct = 100 if wmtd[wh] * price >= b else 80
                    add(f"whbudget:{wh}:{pct}:{_month(today)}", "budget", "critical" if pct == 100 else "warning",
                        f"{wh} is at {wmtd[wh] * price / b:.0%} of its monthly budget", f"${wmtd[wh] * price:,.0f} of ${b:,.0f} so far this month.", wh)
    return {**state, "alerts": (alerts + new)[-100:]}


def deliver(conn_id: int, now: datetime) -> None:
    """Send alerts not yet sent to Teams (the default flow), if alerts are on. Nothing is held while sending."""
    from ..config import AlertsConfig
    from ..connectors.notifiers import NotifyError, PowerAutomateNotifier
    from ..db import session_scope
    from ..settings import get_settings

    with session_scope() as s:
        st = state_of(s, conn_id)
        conn = s.get(DbConnection, conn_id)
        name = conn.name if conn else "Snowflake"
    pending = [a for a in st.get("alerts") or [] if a.get("delivery") is None]
    if not pending:
        return
    notifier = PowerAutomateNotifier(AlertsConfig())
    results = {}
    for a in pending:
        if not settings_of(st)["alerts_on"]:
            results[a["key"]] = "off"
        elif not notifier.configured:
            results[a["key"]] = "no_flow"
        else:
            try:
                notifier.send(alert_payload(a, name, get_settings().portal_url), f"compute:{conn_id}:{a['key']}")
                results[a["key"]] = "sent"
            except NotifyError as exc:
                log.warning("compute alert not sent: %s", exc)
                results[a["key"]] = "failed"
    with session_scope() as s:
        st = state_of(s, conn_id)
        st["alerts"] = [{**a, "delivery": results.get(a["key"], a.get("delivery"))} for a in st.get("alerts") or []]
        save_state(s, conn_id, st)


def alert_payload(a: dict[str, Any], account: str, portal: str) -> dict[str, Any]:
    url = f"{portal.rstrip('/')}/compute"
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.5", "msteams": {"width": "Full"},
            "body": [{"type": "Container", "style": "attention" if a["severity"] == "critical" else "warning", "bleed": True,
                      "items": [{"type": "TextBlock", "text": f"Snowflake compute · {account}", "size": "Small", "weight": "Bolder", "isSubtle": True},
                                {"type": "TextBlock", "text": a["title"], "size": "Large", "weight": "Bolder", "wrap": True}]},
                     {"type": "TextBlock", "text": a["detail"], "wrap": True}],
            "actions": [{"type": "Action.OpenUrl", "title": "Open Compute cost", "url": url}]}
    return {"schema_version": "1.0", "event": "compute.alert", "occurred_at": a["at"], "project": "compute", "project_name": f"Snowflake {account}",
            "incident_id": 0, "status": "open", "severity": a["severity"], "title": a["title"], "summary": a["detail"], "portal_url": url,
            "teams_channel": "", "email_owners": False, "owner_emails": [], "card": card}


# ── savings: what to change, and the script (the bot never runs it) ──────────


def size_index(size: str | None) -> int | None:
    """"X-Small" / "XSMALL" / "x-small" → 0 … "6X-Large" → 9."""
    norm = {x.upper().replace("-", ""): i for i, x in enumerate(SIZES)}
    return norm.get((size or "").upper().replace("-", "").replace(" ", ""))


def _size_step(size: str, step: int) -> str | None:
    i = size_index(size)
    if i is None or not 0 <= i + step < len(SIZES):
        return None
    return SIZES[i + step]


def recommendations(state: dict[str, Any], today: date) -> list[dict[str, Any]]:
    rd = state.get("read") or {}
    price = price_of(state)[0]
    whs = rd.get("warehouses") or {}
    stats = rd.get("wh_stats") or {}
    since = (today - timedelta(days=30)).isoformat()
    metered: dict[str, float] = defaultdict(float)
    compute: dict[str, float] = defaultdict(float)
    for r in rd.get("wh_daily") or []:
        if r["date"] >= since:
            metered[r["wh"]] += r["credits"]
            compute[r["wh"]] += r["compute"]
    used: dict[str, float] = defaultdict(float)
    for r in rd.get("attributed") or []:
        used[r["wh"]] += r["credits"]
    out = []
    for wh in sorted(set(metered) | set(whs)):
        w, st = whs.get(wh) or {}, stats.get(wh) or {}
        idle = max(0.0, compute[wh] - used[wh])
        susp = w.get("auto_suspend")
        if idle * price >= 1 and (not w or susp is None or susp == 0 or susp > 60):
            factor = 0.9 if not susp else max(0.0, min(0.9, 1 - 60 / susp))
            save = idle * factor
            if save * price >= 1:
                out.append({"id": f"suspend:{wh}", "kind": "suspend", "wh": wh, "credits": round(save, 2), "monthly": round(save * price, 2),
                            "title": f"Let {wh} sleep after 1 minute idle",
                            "why": ("It never suspends by itself" if w and not susp else f"It waits {susp // 60} minutes" if susp and susp >= 120 else
                                    f"It waits {susp} seconds" if susp else "It stays on") + f" after its last query: {idle:.1f} credits "
                                    f"(${idle * price:,.0f}) in 30 days were spent with nothing running.",
                            "change": "AUTO_SUSPEND = 60 seconds", "current": susp, "confidence": "high" if w else "medium"})
        size = w.get("size") or st.get("size") or ""
        p90 = st.get("p90_ms")
        if (smaller := _size_step(size, -1)) and (size_index(size) or 0) >= 2 and p90 is not None and p90 < 5000 \
                and not st.get("spill_remote") and (st.get("queued") or 0) < max(1, st.get("queries", 0) * 0.02) and used[wh] * price >= 5:
            save = used[wh] * 0.4
            out.append({"id": f"downsize:{wh}", "kind": "downsize", "wh": wh, "credits": round(save, 2), "monthly": round(save * price, 2),
                        "title": f"Try {wh} one size smaller ({smaller})", "change": f"{size} → {smaller}", "current": size, "to": smaller,
                        "why": f"9 in 10 of its queries finish in under {p90 / 1000:.1f} s, nothing spills and nothing waits: a {smaller} costs half "
                               f"per hour; queries take a little longer, so the saving is usually 30–50%.", "confidence": "medium"})
        queued, n = st.get("queued") or 0, st.get("queries") or 0
        if n >= 50 and (queued / n > 0.1 and (st.get("avg_queue_ms") or 0) > 2000 or (st.get("spill_remote") or 0) > 0):
            bigger = _size_step(size, 1)
            multi = (w.get("max_clusters") or 1) <= 1
            out.append({"id": f"upsize:{wh}", "kind": "upsize", "wh": wh, "credits": 0.0, "monthly": 0.0, "to": bigger, "current": size,
                        "title": f"{wh} is too busy: queries wait" if queued / max(n, 1) > 0.1 else f"{wh} runs out of memory",
                        "change": "allow a second cluster when busy" if multi and queued / max(n, 1) > 0.1 else f"{size} → {bigger}",
                        "why": (f"{queued:,} of {n:,} queries in 14 days had to queue (on average {(st.get('avg_queue_ms') or 0) / 1000:.1f} s). "
                                if queued / max(n, 1) > 0.1 else "Queries spill to remote storage, which is slow. ") +
                               "This costs more while busy, but people wait less; worth it when the delays matter.", "confidence": "medium"})
        if w and not w.get("resource_monitor") and metered[wh] * price >= 20:
            quota = max(1, round(metered[wh] * 1.2))
            out.append({"id": f"monitor:{wh}", "kind": "monitor", "wh": wh, "credits": 0.0, "monthly": 0.0, "quota": quota,
                        "title": f"Put a monthly spending cap on {wh}", "change": f"cap {quota} credits a month (≈ ${quota * price:,.0f})",
                        "why": f"Nothing limits what it can spend. A resource monitor warns at 80% and suspends at 100% of {quota} credits "
                               "(last month's use plus 20%), so a runaway job can't burn the month.", "confidence": "high"})
    out.sort(key=lambda r: (-r["monthly"], r["kind"]))
    return out


def _qi(name: str) -> str:
    return '"' + safe_name(name).replace('"', '""') + '"'


def script(state: dict[str, Any], ids: list[str], by: str, account: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    recs = {r["id"]: r for r in recommendations(state, now.date())}
    chosen = [recs[i] for i in dict.fromkeys(ids) if i in recs]
    skipped = [f"{i}: not a current suggestion (the data may have changed)" for i in ids if i not in recs]
    lines = [f"-- Generated by AlertBot on {now:%Y-%m-%d %H:%M} UTC for {by} · Snowflake account {account}",
             "-- AlertBot never runs this. Review it, then run it yourself in a Snowsight worksheet with a role allowed to change",
             "-- warehouses (resource monitors need ACCOUNTADMIN). Each change says how to undo it."]
    if skipped:
        lines += ["--", "-- Left out:"] + [f"--   {s}" for s in skipped]
    total = sum(r["monthly"] for r in chosen)
    if total:
        lines.append(f"-- Estimated saving: about ${total:,.2f} a month.")
    for r in chosen:
        wh = _qi(r["wh"])
        lines += ["", f"-- {r['title']}: {r['change']}"]
        if r["kind"] == "suspend":
            lines.append(f"ALTER WAREHOUSE {wh} SET AUTO_SUSPEND = 60;")
            undo = f"AUTO_SUSPEND = {r['current']}" if r.get("current") else "AUTO_SUSPEND = 0"
            lines.append(f"-- undo: ALTER WAREHOUSE {wh} SET {undo};")
        elif r["kind"] == "downsize":
            lines.append(f"ALTER WAREHOUSE {wh} SET WAREHOUSE_SIZE = '{r['to'].upper()}';")
            lines.append(f"-- undo: ALTER WAREHOUSE {wh} SET WAREHOUSE_SIZE = '{r['current'].upper()}';")
        elif r["kind"] == "upsize":
            if "cluster" in r["change"]:
                lines.append(f"ALTER WAREHOUSE {wh} SET MIN_CLUSTER_COUNT = 1 MAX_CLUSTER_COUNT = 2 SCALING_POLICY = 'STANDARD';  -- Enterprise edition")
                lines.append(f"-- undo: ALTER WAREHOUSE {wh} SET MAX_CLUSTER_COUNT = 1;")
                if r.get("to"):
                    lines.append(f"-- or, on Standard edition: ALTER WAREHOUSE {wh} SET WAREHOUSE_SIZE = '{r['to'].upper()}';")
            elif r.get("to"):
                lines.append(f"ALTER WAREHOUSE {wh} SET WAREHOUSE_SIZE = '{r['to'].upper()}';")
                lines.append(f"-- undo: ALTER WAREHOUSE {wh} SET WAREHOUSE_SIZE = '{r['current'].upper()}';")
        elif r["kind"] == "monitor":
            mon = _qi(f"ALERTBOT_{r['wh']}_MONTHLY"[:250])
            lines.append(f"CREATE RESOURCE MONITOR IF NOT EXISTS {mon} WITH CREDIT_QUOTA = {r['quota']} FREQUENCY = MONTHLY "
                         f"START_TIMESTAMP = IMMEDIATELY TRIGGERS ON 80 PERCENT DO NOTIFY ON 100 PERCENT DO SUSPEND;")
            lines.append(f"ALTER WAREHOUSE {wh} SET RESOURCE_MONITOR = {mon};")
            lines.append(f"-- undo: ALTER WAREHOUSE {wh} UNSET RESOURCE_MONITOR; DROP RESOURCE MONITOR {mon};")
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", f"snowflake-compute-{len(chosen)}-changes-{now:%Y%m%d}")
    return {"sql": "\n".join(lines) + "\n", "filename": f"{name}.sql", "items": len(chosen), "monthly": round(total, 2), "skipped": skipped}


# ── the page ──────────────────────────────────────────────────────────────────


def snowsight(account: str, query_id: str) -> str | None:
    """A link to a query in Snowsight (where people with access can see it; its text never comes to the bot)."""
    acct = (account or "").lower().removesuffix(".snowflakecomputing.com")
    if not query_id or not re.fullmatch(r"[0-9a-f-]{10,64}", query_id):
        return None
    if "." in acct:  # locator.region(.cloud)
        loc, region = acct.split(".", 1)
        return f"https://app.snowflake.com/{region}/{loc}/#/compute/history/queries/{query_id}/detail"
    if "-" in acct:  # org-account
        org, name = acct.split("-", 1)
        return f"https://app.snowflake.com/{org}/{name}/#/compute/history/queries/{query_id}/detail"
    return None


def view(session: Session, conn: DbConnection, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    today = now.date()
    st = state_of(session, conn.id)
    s = settings_of(st)
    rd = st.get("read") or {}
    price, price_from, price_note = price_of(st)
    sp = spend(st, today)
    money = lambda c: round((c or 0) * price, 2)  # noqa: E731
    days = [(today - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    svc: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rd.get("daily") or []:
        svc[r["date"]][r["service"]] += r["billed"]
    wh_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rd.get("wh_daily") or []:
        wh_day[r["date"]][r["wh"]] += r["credits"]
    chart = [{"date": d, "credits": round(sum(svc[d].values()), 3), "cost": money(sum(svc[d].values())),
              "warehouses": {k: money(v) for k, v in wh_day[d].items()},
              "other": money(sum(v for k, v in svc[d].items() if k not in ("WAREHOUSE_METERING", "CLOUD_SERVICES")))} for d in days]
    since = (today - timedelta(days=30)).isoformat()
    metered, comp, used = defaultdict(float), defaultdict(float), defaultdict(float)
    for r in rd.get("wh_daily") or []:
        if r["date"] >= since:
            metered[r["wh"]] += r["credits"]
            comp[r["wh"]] += r["compute"]
    for r in rd.get("attributed") or []:
        used[r["wh"]] += r["credits"]
    heat: dict[str, list[list[float]]] = {}
    for h in rd.get("hourly") or []:
        t = _dt(h["at"])
        if now - t <= timedelta(days=7):
            grid = heat.setdefault(h["wh"], [[0.0] * 24 for _ in range(7)])
            grid[t.weekday()][t.hour] += h["credits"]
    whs = rd.get("warehouses") or {}
    warehouses = []
    for wh in sorted(set(metered) | set(whs), key=lambda w: -metered.get(w, 0)):
        w, stt = whs.get(wh) or {}, (rd.get("wh_stats") or {}).get(wh) or {}
        idle = max(0.0, comp[wh] - used[wh])
        warehouses.append({"name": wh, "size": w.get("size") or stt.get("size"), "state": w.get("state"), "auto_suspend": w.get("auto_suspend"),
                           "max_clusters": w.get("max_clusters"), "resource_monitor": w.get("resource_monitor"), "credits": round(metered[wh], 3),
                           "cost": money(metered[wh]), "idle_credits": round(idle, 3), "idle_cost": money(idle),
                           "idle_share": round(idle / comp[wh], 3) if comp[wh] else None, "queries": stt.get("queries"), "p90_ms": stt.get("p90_ms"),
                           "queued": stt.get("queued"), "credits_per_hour": 2 ** i if (i := size_index(w.get("size") or stt.get("size"))) is not None else None,
                           "heat": heat.get(wh), "budget": (s.get("warehouse_budgets") or {}).get(wh)})
    users: dict[str, dict[str, float]] = defaultdict(lambda: {"credits": 0.0, "queries": 0})
    for r in rd.get("users") or []:
        users[r["user"]]["credits"] += r["credits"]
        users[r["user"]]["queries"] += r["queries"]
    pipeline_users = set(rd.get("pipeline_users") or [])
    by_user = sorted(({"name": u, "credits": round(v["credits"], 3), "cost": money(v["credits"]), "queries": int(v["queries"]),
                       "service": u in pipeline_users} for u, v in users.items()), key=lambda x: -x["credits"])[:30]
    tags = rd.get("tags") or []
    own = [t for t in tags if t["tag"].lower().startswith("alertbot")]
    own_credits = sum(t["credits"] for t in own)
    recs = recommendations(st, today)
    pipes = st.get("pipelines") or {}
    storage = rd.get("storage") or []
    month_bytes = [r["bytes"] for r in storage if r["date"].startswith(_month(today))]
    tb = (statistics.fmean(month_bytes) / 1024 ** 4) if month_bytes else None
    storage_price = s["storage_tb"] or STORAGE_TB
    storage_month = round(tb * storage_price, 2) if tb is not None else None
    alerts = sorted(st.get("alerts") or [], key=lambda a: a["at"], reverse=True)
    yday_change = (sp["yesterday"] / sp["usual_day"] - 1) if sp["usual_day"] else None
    return {
        "id": conn.id, "name": conn.name, "account": conn.host, "scanned_at": st.get("scanned_at"), "running_at": st.get("running_at"),
        "error": st.get("error"), "errors": rd.get("errors") or {},
        "price": price, "price_from": price_from, "price_note": price_note, "storage_tb_price": storage_price, "settings": s,
        "summary": {"yesterday": money(sp["yesterday"]), "yesterday_credits": round(sp["yesterday"], 3), "usual_day": money(sp["usual_day"]),
                    "yesterday_change": round(yday_change, 3) if yday_change is not None else None, "mtd": money(sp["mtd"]),
                    "mtd_credits": round(sp["mtd"], 3), "last_month": money(sp["last_month"]), "forecast": money(sp["forecast"]),
                    "budget": s["budget"], "storage_month": storage_month, "storage_tb": round(tb, 4) if tb is not None else None,
                    "bill_forecast": round(money(sp["forecast"]) + (storage_month or 0), 2),
                    "idle_cost": round(sum(w["idle_cost"] for w in warehouses), 2), "could_save": round(sum(r["monthly"] for r in recs), 2),
                    "own_cost": money(own_credits), "own_credits": round(own_credits, 4),
                    "own_queries": sum(t["queries"] for t in own)},
        "chart": chart,
        "services": sorted(({"service": k, "name": SERVICE_NAMES.get(k, k.replace("_", " ").title()), "credits": round(v, 3), "cost": money(v)}
                            for k, v in _sum_by(rd.get("daily") or [], since).items()), key=lambda x: -x["credits"]),
        "warehouses": warehouses, "users": by_user,
        "roles": [{"name": r["role"], "credits": round(r["credits"], 3), "cost": money(r["credits"]), "queries": r["queries"]} for r in (rd.get("roles") or [])[:30]],
        "tags": [{"name": t["tag"] or "(no tag)", "credits": round(t["credits"], 3), "cost": money(t["credits"]), "queries": t["queries"],
                  "alertbot": t["tag"].lower().startswith("alertbot")} for t in tags[:30]],
        "shapes": [{**q, "cost": money(q["credits"]), "per_run": money(q["credits"] / q["runs"]) if q["runs"] else None,
                    "monthly": money(q["credits"]), "link": snowsight(conn.host or "", q["query_id"])} for q in rd.get("shapes") or []],
        "pipelines": {**pipes, "rows": [{**p, "cost": money(p["credits"]), "per_run_cost": money(p["per_run"]) if p.get("per_run") else None}
                                        for p in pipes.get("rows") or []], "unlinked_cost": money(pipes.get("unlinked")),
                      "users": sorted(pipeline_users), "users_set": s["pipeline_users"] is not None},
        "recommendations": recs,
        "running": [{**q, "link": snowsight(conn.host or "", q["id"]), "long": q["seconds"] >= s["runaway_minutes"] * 60}
                    for q in sorted(rd.get("running") or [], key=lambda q: -q["seconds"])[:20]],
        "alerts": alerts[:30],
    }


def _sum_by(daily: list[dict[str, Any]], since: str) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for r in daily:
        if r["date"] >= since:
            out[r["service"]] += r["billed"]
    return out


def report(session: Session, user: User, now: datetime | None = None) -> dict[str, Any]:
    conns = list(session.scalars(select(DbConnection).where(DbConnection.user_id == user.id, DbConnection.kind == "snowflake").order_by(DbConnection.name)))
    return {"accounts": [view(session, c, now) for c in conns], "generated_at": iso(now or utcnow()), "sizes": CREDITS_PER_HOUR,
            "list_price": LIST_PRICE}


# ── for the morning report and the assistant ─────────────────────────────────


def morning_line(session: Session, project: Project, now: datetime) -> str | None:
    """"Snowflake compute yesterday: $142 (+18% vs a usual day), mostly COMPUTE_WH. This month $2,310 (budget $3,000).\""""
    members = [u for (u,) in session.execute(select(UserProject.user_id).where(UserProject.project_id == project.id))]
    conns = list(session.scalars(select(DbConnection).where(DbConnection.user_id.in_(members or [-1]), DbConnection.kind == "snowflake")))
    parts = []
    for c in conns:
        v = view(session, c, now)
        if not v["scanned_at"]:
            continue
        sm = v["summary"]
        top = max(((w["name"], w["cost"]) for w in v["warehouses"]), key=lambda x: x[1], default=None)
        ch = f" ({sm['yesterday_change']:+.0%} vs a usual day)" if sm["yesterday_change"] is not None else ""
        parts.append(f"Snowflake compute {c.name}: yesterday ${sm['yesterday']:,.0f}{ch}" + (f", mostly {top[0]}" if top and top[1] else "")
                     + f"; this month ${sm['mtd']:,.0f}" + (f" of ${sm['budget']:,.0f} budget" if sm["budget"] else f", heading for ${sm['forecast']:,.0f}"))
    return " · ".join(parts) or None


def explain_change(session: Session, user: User, now: datetime | None = None) -> str | None:
    """Plain words for "why did Snowflake spend change?": yesterday against a usual day, by warehouse and by user."""
    now = now or utcnow()
    out = []
    for c in session.scalars(select(DbConnection).where(DbConnection.user_id == user.id, DbConnection.kind == "snowflake")):
        st = state_of(session, c.id)
        if not st.get("scanned_at"):
            continue
        v = view(session, c, now)
        sm, price = v["summary"], v["price"]
        rd = st.get("read") or {}
        yday = (now.date() - timedelta(days=1)).isoformat()
        week = {(now.date() - timedelta(days=i)).isoformat() for i in range(2, 9)}
        whd = _change(rd.get("wh_daily") or [], "wh", yday, week, price)
        usd = _change(rd.get("users") or [], "user", yday, week, price)
        line = f"{c.name}: yesterday ${sm['yesterday']:,.2f}" + (f" against a usual ${sm['usual_day']:,.2f} a day" if sm["usual_day"] is not None else "") + "."
        if whd and abs(whd[0][1]) >= 1:
            line += " By warehouse: " + ", ".join(f"{k} {'+' if v >= 0 else '-'}${abs(v):,.0f}" for k, v in whd if abs(v) >= 0.5) + "."
        if usd and abs(usd[0][1]) >= 1:
            line += " By who: " + ", ".join(f"{k} {'+' if v >= 0 else '-'}${abs(v):,.0f}" for k, v in usd if abs(v) >= 0.5) + "."
        line += f" This month so far ${sm['mtd']:,.0f}, heading for about ${sm['forecast']:,.0f}."
        out.append(line)
    return " ".join(out) or None


def _change(rows: list[dict[str, Any]], key: str, yday: str, week: set[str], price: float) -> list[tuple[str, float]]:
    """Each name's $ yesterday minus its average day over the week before, biggest changes first."""
    y: dict[str, float] = defaultdict(float)
    b: dict[str, float] = defaultdict(float)
    for r in rows:
        if r["date"] == yday:
            y[r[key]] += r["credits"]
        elif r["date"] in week:
            b[r[key]] += r["credits"] / 7
    return sorted(((k, (y[k] - b[k]) * price) for k in set(y) | set(b)), key=lambda kv: -abs(kv[1]))[:3]


def pipeline_costs(session: Session, user_id: int) -> dict[tuple[str, str], dict[str, Any]]:
    """(project key, pipeline) → its Snowflake compute: the last 30 days in $, and per run (from the person's accounts)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for c in session.scalars(select(DbConnection).where(DbConnection.user_id == user_id, DbConnection.kind == "snowflake")):
        st = state_of(session, c.id)
        price = price_of(st)[0]
        for r in (st.get("pipelines") or {}).get("rows") or []:
            if r["credits"]:
                cur = out.setdefault((r["project"], r["pipeline"]), {"monthly": 0.0, "per_run": None, "how": r["how"]})
                cur["monthly"] = round(cur["monthly"] + r["credits"] * price, 2)
                cur["per_run"] = round((r["per_run"] or 0) * price, 2) if r["per_run"] else cur["per_run"]
    return out
