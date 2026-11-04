"""Snowflake Cortex AI usage: what Cortex costs (Agents, Cortex Code, AI SQL functions, Analyst, Search, Intelligence,
Document AI, fine-tuning, the REST API), who and what used it, and on which models.

Two layers, never mixed up:
    the bill        SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY, the AI service types (CREDITS_BILLED per day). This is
                    what Snowflake charges, so every total and every dollar on the page comes from here.
    the detail      the Cortex usage views (CORTEX_AGENT_USAGE_HISTORY, CORTEX_AISQL_USAGE_HISTORY, CORTEX_CODE_*_USAGE_HISTORY,
                    CORTEX_ANALYST_USAGE_HISTORY, …): credits, tokens and requests by product, user, model and agent. They
                    explain where the bill went; the page says how much of the bill they account for.
Each detail view is optional: one this account doesn't have, or this role can't read, is listed as unavailable, never guessed.
Days are UTC days, like the bill. Dollars are credits × the price per credit set on the Compute cost page. Read-only and
metadata only: names, counts, credits and tokens; never a prompt, a response or a query's text.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import BotState, DbConnection
from ..timeutil import iso, utcnow
from . import sql as T
from .client import DbError
from .privacy import RawDataRejected, safe_name
from .storage import _dt, _num

KEY = "cortex:conn:{}"
SCAN_EVERY = timedelta(hours=1)
DAYS = 365  # ACCOUNT_USAGE keeps a year
PERIODS = (30, 90, 365)
AU = "SNOWFLAKE.ACCOUNT_USAGE"

# The bill: which service types are Cortex / AI, and what to call them
AI_SERVICE = re.compile(r"^(AI_|CORTEX|SNOWFLAKE_COCO|SNOWFLAKE_INTELLIGENCE|DOCUMENT_AI|COPILOT)", re.I)
SERVICE_NAMES = {
    "CORTEX_AGENTS": "Cortex Agents", "SNOWFLAKE_COCO_SNOWSIGHT": "Cortex Code in Snowsight", "SNOWFLAKE_COCO_CLI": "Cortex Code CLI",
    "SNOWFLAKE_COCO_DESKTOP": "Cortex Code desktop", "AI_FUNCTIONS": "AI SQL functions", "AI_SERVICES": "AI services",
    "AI_INFERENCE": "Cortex REST API", "SNOWFLAKE_INTELLIGENCE": "Snowflake Intelligence", "CORTEX_SEARCH": "Cortex Search",
    "DOCUMENT_AI": "Document AI", "CORTEX_ANALYST": "Cortex Analyst", "COPILOT": "Snowflake Copilot",
}
SERVICE_HELP = {
    "CORTEX_AGENTS": "Agents that plan, call tools and answer: each request's tokens.",
    "SNOWFLAKE_COCO_SNOWSIGHT": "Cortex Code, the coding assistant inside Snowsight.",
    "SNOWFLAKE_COCO_CLI": "Cortex Code from the command line.", "SNOWFLAKE_COCO_DESKTOP": "Cortex Code in the desktop app.",
    "AI_FUNCTIONS": "AI_COMPLETE, AI_CLASSIFY, SUMMARIZE and the other AI functions called from SQL.",
    "AI_SERVICES": "Cortex AI services billed together (Analyst, Search, Document AI and older AI functions).",
    "AI_INFERENCE": "Models called through the Cortex REST API.", "SNOWFLAKE_INTELLIGENCE": "Snowflake Intelligence, the chat over your data.",
}


def service_name(s: str) -> str:
    return SERVICE_NAMES.get(s.upper(), s.replace("_", " ").title())


def _q(name: str, sql: str) -> T.MetaQuery:
    return T.MetaQuery(name, sql, {})


def _since(col: str) -> str:
    return f"{col} >= DATEADD(day, -{DAYS}, CURRENT_TIMESTAMP())"


def _day_of(col: str) -> str:
    return f"TO_DATE(CONVERT_TIMEZONE('UTC', {col}))"  # a UTC day, like the bill's USAGE_DATE


def queries() -> dict[str, T.MetaQuery]:
    """Every source: the bill, then one query per Cortex product (aggregated in Snowflake: one row per day and name)."""
    d = _day_of
    users = f"LEFT JOIN {AU}.USERS u ON u.USER_ID = x.USER_ID"
    return {
        "bill": _q("cortex_bill", f"SELECT USAGE_DATE, SERVICE_TYPE, SUM(CREDITS_USED), SUM(CREDITS_BILLED) FROM {AU}.METERING_DAILY_HISTORY "
                                  f"WHERE USAGE_DATE >= DATEADD(day, -{DAYS}, CURRENT_DATE()) GROUP BY 1, 2"),
        "agents": _q("cortex_agents", f"SELECT {d('START_TIME')}, AGENT_NAME, USER_NAME, SUM(TOKEN_CREDITS), SUM(TOKENS), COUNT(*) "
                                      f"FROM {AU}.CORTEX_AGENT_USAGE_HISTORY WHERE {_since('START_TIME')} GROUP BY 1, 2, 3"),
        "intelligence": _q("cortex_intelligence", f"SELECT {d('START_TIME')}, SNOWFLAKE_INTELLIGENCE_NAME, USER_NAME, SUM(TOKEN_CREDITS), SUM(TOKENS), COUNT(*) "
                                                  f"FROM {AU}.SNOWFLAKE_INTELLIGENCE_USAGE_HISTORY WHERE {_since('START_TIME')} GROUP BY 1, 2, 3"),
        **{f"code_{surface}": _q(f"cortex_code_{surface}", f"SELECT {d('USAGE_TIME')}, USER_NAME, SUM(TOKEN_CREDITS), SUM(TOKENS), COUNT(*) "
                                                            f"FROM {AU}.CORTEX_CODE_{surface.upper()}_USAGE_HISTORY WHERE {_since('USAGE_TIME')} GROUP BY 1, 2")
           for surface in ("snowsight", "cli", "desktop")},
        "aisql": _q("cortex_aisql", f"SELECT {d('x.USAGE_TIME')}, x.FUNCTION_NAME, x.MODEL_NAME, u.NAME, SUM(x.TOKEN_CREDITS), SUM(x.TOKENS), "
                                    f"COUNT(DISTINCT x.QUERY_ID) FROM {AU}.CORTEX_AISQL_USAGE_HISTORY x {users} "
                                    f"WHERE {_since('x.USAGE_TIME')} GROUP BY 1, 2, 3, 4"),
        # older accounts: the AI functions' own view (no user)
        "functions": _q("cortex_functions", f"SELECT {d('START_TIME')}, FUNCTION_NAME, MODEL_NAME, SUM(TOKEN_CREDITS), SUM(TOKENS) "
                                            f"FROM {AU}.CORTEX_FUNCTIONS_USAGE_HISTORY WHERE {_since('START_TIME')} GROUP BY 1, 2, 3"),
        "analyst": _q("cortex_analyst", f"SELECT {d('START_TIME')}, USERNAME, SUM(CREDITS), SUM(REQUEST_COUNT) "
                                        f"FROM {AU}.CORTEX_ANALYST_USAGE_HISTORY WHERE {_since('START_TIME')} GROUP BY 1, 2"),
        "search": _q("cortex_search", f"SELECT {d('USAGE_DATE')}, SERVICE_NAME, CONSUMPTION_TYPE, SUM(CREDITS), SUM(TOKENS) "
                                      f"FROM {AU}.CORTEX_SEARCH_DAILY_USAGE_HISTORY WHERE {_since('USAGE_DATE')} GROUP BY 1, 2, 3"),
        "documents": _q("cortex_documents", f"SELECT {d('START_TIME')}, FUNCTION_NAME, SUM(CREDITS_USED), SUM(PAGE_COUNT), SUM(DOCUMENT_COUNT) "
                                            f"FROM {AU}.CORTEX_DOCUMENT_PROCESSING_USAGE_HISTORY WHERE {_since('START_TIME')} GROUP BY 1, 2"),
        "document_ai": _q("cortex_document_ai", f"SELECT {d('START_TIME')}, OPERATION_NAME, SUM(CREDITS_USED), SUM(PAGE_COUNT), SUM(DOCUMENT_COUNT) "
                                                f"FROM {AU}.DOCUMENT_AI_USAGE_HISTORY WHERE {_since('START_TIME')} GROUP BY 1, 2"),
        "fine_tuning": _q("cortex_fine_tuning", f"SELECT {d('START_TIME')}, MODEL_NAME, SUM(TOKEN_CREDITS), SUM(TOKENS) "
                                                f"FROM {AU}.CORTEX_FINE_TUNING_USAGE_HISTORY WHERE {_since('START_TIME')} GROUP BY 1, 2"),
        "guardrails": _q("cortex_guardrails", f"SELECT {d('USAGE_TIME')}, USER_NAME, SUM(TOKEN_CREDITS), SUM(TOKENS), COUNT(*) "
                                              f"FROM {AU}.CORTEX_AI_GUARDRAILS_USAGE_HISTORY WHERE {_since('USAGE_TIME')} GROUP BY 1, 2"),
        "throughput": _q("cortex_throughput", f"SELECT {d('INTERVAL_START_TIME')}, MODEL_NAME, SUM(PTU_CREDITS) "
                                              f"FROM {AU}.CORTEX_PROVISIONED_THROUGHPUT_USAGE_HISTORY WHERE {_since('INTERVAL_START_TIME')} GROUP BY 1, 2"),
        # the REST API view has tokens but no credits: its cost is the bill's AI inference line
        "rest": _q("cortex_rest", f"SELECT {d('x.START_TIME')}, x.MODEL_NAME, u.NAME, SUM(x.TOKENS), COUNT(*) FROM {AU}.CORTEX_REST_API_USAGE_HISTORY x {users} "
                                  f"WHERE {_since('x.START_TIME')} GROUP BY 1, 2, 3"),
    }


# What each detail source is, for people
PRODUCTS: dict[str, tuple[str, str]] = {
    "agents": ("Cortex Agents", "Agents answering questions and calling tools"),
    "intelligence": ("Snowflake Intelligence", "Chat over your data, for business users"),
    "code": ("Cortex Code", "The coding assistant (Snowsight, CLI, desktop)"),
    "aisql": ("AI SQL functions", "AI_COMPLETE, AI_CLASSIFY, SUMMARIZE… called from SQL"),
    "analyst": ("Cortex Analyst", "Questions in plain English turned into SQL"),
    "search": ("Cortex Search", "Search services: serving and indexing"),
    "documents": ("Document processing", "Reading pages and extracting fields from documents"),
    "fine_tuning": ("Fine-tuning", "Training custom versions of models"),
    "guardrails": ("Guardrails", "Checking prompts and answers for safety"),
    "throughput": ("Provisioned throughput", "Reserved model capacity"),
    "rest": ("Cortex REST API", "Models called over HTTPS (tokens only: its cost is on the bill)"),
}


def _name(v: Any) -> str | None:
    try:
        return safe_name(str(v)) if v is not None and str(v).strip() else None
    except RawDataRejected:
        return None


def _day(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        v = v.date()
    return v.isoformat() if isinstance(v, date) else str(v)[:10]


def read(client: Any) -> dict[str, Any]:
    """The bill and every readable Cortex view. A view that isn't there (or can't be read) is noted, never invented."""
    q = queries()
    out: dict[str, Any] = {"errors": {}, "sources": {}}

    def run(key: str) -> list[Any] | None:
        try:
            rows = list(client.meta(q[key], 50000))
            out["sources"][key] = "ok"
            return rows
        except DbError as exc:
            out["sources"][key] = "unavailable"
            out["errors"][key] = str(exc)[:200]
            return None

    bill = run("bill")
    if bill is None:  # the bill is the one thing that must be readable
        from .privacy import SafeError

        raise DbError(SafeError("no_usage", "This login can't read SNOWFLAKE.ACCOUNT_USAGE. Grant its role IMPORTED PRIVILEGES on the SNOWFLAKE database."))
    out["bill"] = [{"date": _day(dt), "service": str(s).upper(), "credits": _num(c) or 0.0, "billed": _num(b) if b is not None else _num(c) or 0.0}
                   for dt, s, c, b in bill if s and AI_SERVICE.match(str(s))]
    ev: list[dict[str, Any]] = []  # one event row per day × product × names

    def add(product: str, day: Any, credits: Any = None, tokens: Any = None, requests: Any = None, **names: Any) -> None:
        ev.append({"product": product, "date": _day(day), "credits": _num(credits), "tokens": _num(tokens), "requests": _num(requests),
                   **{k: _name(v) for k, v in names.items()}})

    for dt, agent, user, c, t, n in run("agents") or []:
        add("agents", dt, c, t, n, agent=agent, user=user)
    for dt, si, user, c, t, n in run("intelligence") or []:
        add("intelligence", dt, c, t, n, agent=si, user=user)
    for surface in ("snowsight", "cli", "desktop"):
        for dt, user, c, t, n in run(f"code_{surface}") or []:
            add("code", dt, c, t, n, user=user, surface={"snowsight": "Snowsight", "cli": "CLI", "desktop": "Desktop"}[surface])
    aisql = run("aisql")
    for dt, fn, model, user, c, t, n in aisql or []:
        add("aisql", dt, c, t, n, function=fn, model=model, user=user)
    if aisql is None:  # only when the newer view isn't there, so nothing is counted twice
        for dt, fn, model, c, t in run("functions") or []:
            add("aisql", dt, c, t, None, function=fn, model=model)
    for dt, user, c, n in run("analyst") or []:
        add("analyst", dt, c, None, n, user=user)
    for dt, svc, kind, c, t in run("search") or []:
        add("search", dt, c, t, None, service=svc, kind=kind)
    for dt, fn, c, pages, docs in run("documents") or []:
        add("documents", dt, c, None, docs, function=fn, pages=None)
        ev[-1]["pages"] = _num(pages)
    for dt, op, c, pages, docs in run("document_ai") or []:
        add("documents", dt, c, None, docs, function=op)
        ev[-1]["pages"] = _num(pages)
    for dt, model, c, t in run("fine_tuning") or []:
        add("fine_tuning", dt, c, t, None, model=model)
    for dt, user, c, t, n in run("guardrails") or []:
        add("guardrails", dt, c, t, n, user=user)
    for dt, model, c in run("throughput") or []:
        add("throughput", dt, c, None, None, model=model)
    for dt, model, user, t, n in run("rest") or []:
        add("rest", dt, None, t, n, model=model, user=user)
    out["events"] = ev
    return out


# ── kept per account ──────────────────────────────────────────────────────────


def state_of(session: Session, conn_id: int) -> dict[str, Any]:
    row = session.get(BotState, KEY.format(conn_id))
    return dict(row.value) if row and row.value else {}


def save_state(session: Session, conn_id: int, state: dict[str, Any]) -> None:
    row = session.get(BotState, KEY.format(conn_id)) or BotState(key=KEY.format(conn_id), value={})
    row.value, row.updated_at = state, utcnow()
    session.add(row)
    session.flush()


def scan(session: Session, conn: DbConnection, now: datetime | None = None) -> dict[str, Any]:
    from .service import client_of

    now = now or utcnow()
    prev = state_of(session, conn.id)
    try:
        got, error = read(client_of(conn)), None
    except (DbError, Exception) as exc:  # noqa: BLE001 - shown on the page; the last good read stays
        got, error = None, str(exc)[:300] if isinstance(exc, DbError) else "Snowflake couldn't be read just now"
    state = {**prev, "error": error, "error_at": iso(now)} if got is None else {**prev, "read": got, "scanned_at": iso(now), "error": None}
    save_state(session, conn.id, state)
    return state


def scan_all(now: datetime | None = None) -> int:
    """Worker: each connected Snowflake account, hourly."""
    from ..db import session_scope

    now = now or utcnow()
    with session_scope() as s:
        ids = [c.id for c in s.scalars(select(DbConnection).where(DbConnection.kind == "snowflake"))]
    n = 0
    for cid in ids:
        with session_scope() as s:
            st = state_of(s, cid)
            if st.get("scanned_at") and now - _dt(st["scanned_at"]) < SCAN_EVERY:
                continue
            conn = s.get(DbConnection, cid)
            if conn is None:
                continue
            scan(s, conn, now)
            n += 1
    return n


# ── the page ──────────────────────────────────────────────────────────────────


def view(session: Session, conn: DbConnection, now: datetime | None = None) -> dict[str, Any]:
    """Everything the Cortex AI usage page shows for one account: the month (from the bill), and for each period (30 days,
    90 days, 12 months) the bill by service and the detail by product, user, model and agent. Dollars are credits × the
    Compute cost page's price per credit."""
    from . import compute as C

    now = now or utcnow()
    st = state_of(session, conn.id)
    price, price_from, price_note = C.price_of(C.state_of(session, conn.id))
    rd = st.get("read") or {}
    today = now.date()
    first = today.replace(day=1)
    prev_first = (first - timedelta(days=1)).replace(day=1)
    days = [(today - timedelta(days=i)).isoformat() for i in range(DAYS - 1, -1, -1)]

    # the bill, day by day and by service
    bill = rd.get("bill") or []
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in bill:
        by_day[r["date"]][r["service"]] += r["billed"]
    total_day = {d: sum(v.values()) for d, v in by_day.items()}
    chart = [{"date": d, "credits": total_day.get(d, 0.0), "cost": total_day.get(d, 0.0) * price,
              "services": {sv: v * price for sv, v in by_day.get(d, {}).items()}} for d in days]

    def sum_days(pred: Any) -> float:
        return sum(v for d, v in total_day.items() if pred(d))

    yday = (today - timedelta(days=1)).isoformat()
    mtd = sum_days(lambda d: d >= first.isoformat())
    last_month = sum_days(lambda d: prev_first.isoformat() <= d < first.isoformat())
    next_month = (first + timedelta(days=32)).replace(day=1)
    days_in, done = (next_month - first).days, today.day - 1  # today isn't complete
    recent = [total_day.get((today - timedelta(days=i)).isoformat(), 0.0) for i in range(1, 8)]
    pace = statistics.fmean(recent) if any(recent) else (mtd / done if done else 0.0)
    ev = rd.get("events") or []
    used = [d for d in days if total_day.get(d)]

    def group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        g: dict[str, dict[str, float]] = defaultdict(lambda: {"credits": 0.0, "tokens": 0.0, "requests": 0.0})
        for e in rows:
            k = e.get(key)
            if not k:
                continue
            g[k]["credits"] += e["credits"] or 0.0
            g[k]["tokens"] += e["tokens"] or 0.0
            g[k]["requests"] += e["requests"] or 0.0
        return sorted(({"name": k, **v, "cost": v["credits"] * price} for k, v in g.items()), key=lambda r: (-r["credits"], -r["tokens"]))

    def period(n: int) -> dict[str, Any]:
        since = (today - timedelta(days=n)).isoformat()
        before = (today - timedelta(days=2 * n)).isoformat()
        services: dict[str, float] = defaultdict(float)
        for r in bill:
            if r["date"] >= since:
                services[r["service"]] += r["billed"]
        e = [x for x in ev if x["date"] and x["date"] >= since]
        products = []
        for pid, (name, what) in PRODUCTS.items():
            rows = [x for x in e if x["product"] == pid]
            if not rows:
                continue
            daily: dict[str, float] = defaultdict(float)
            for x in rows:
                daily[x["date"]] += x["credits"] or 0.0
            credits = sum(x["credits"] or 0.0 for x in rows)
            products.append({
                "id": pid, "name": name, "what": what, "has_credits": pid != "rest", "credits": credits, "cost": credits * price,
                "tokens": sum(x["tokens"] or 0.0 for x in rows), "requests": sum(x["requests"] or 0.0 for x in rows),
                "pages": sum(x.get("pages") or 0.0 for x in rows) or None, "active_days": len(daily),
                "first": min(daily), "last": max(daily),
                "users": group(rows, "user")[:8], "models": group(rows, "model")[:8], "agents": group(rows, "agent")[:8],
                "functions": group(rows, "function")[:8], "surfaces": group(rows, "surface"), "services": group(rows, "service")[:8],
            })
        products.sort(key=lambda x: (-x["credits"], -x["tokens"]))
        billed = sum(services.values())
        detail = sum(x["credits"] for x in products)
        return {
            "days": n, "credits": billed, "cost": billed * price,
            "prev_cost": sum_days(lambda d: before <= d < since) * price,
            "active_days": len([d for d in used if d >= since]),
            "tokens": sum(x["tokens"] or 0.0 for x in e), "requests": sum(x["requests"] or 0.0 for x in e),
            "users_n": len({x["user"] for x in e if x.get("user")}), "models_n": len({x["model"] for x in e if x.get("model")}),
            "detail_credits": detail, "covered": (detail / billed) if billed else None,
            "services": sorted(({"service": sv, "name": service_name(sv), "help": SERVICE_HELP.get(sv, "A Cortex AI service on your bill."),
                                 "credits": v, "cost": v * price} for sv, v in services.items()), key=lambda r: -r["credits"]),
            "products": products, "users": group(e, "user")[:25], "models": group(e, "model")[:25], "agents": group(e, "agent")[:25],
        }

    periods = {str(n): period(n) for n in PERIODS}
    default = next((str(n) for n in PERIODS if periods[str(n)]["credits"] > 0 or periods[str(n)]["tokens"] > 0), "30")
    sources = rd.get("sources") or {}
    return {
        "id": conn.id, "name": conn.name, "account": conn.host, "scanned_at": st.get("scanned_at"), "error": st.get("error"),
        "price": price, "price_from": price_from, "price_note": price_note,
        "month": {"mtd_credits": mtd, "mtd": mtd * price, "forecast": (mtd + pace * (days_in - done)) * price, "last_month": last_month * price,
                  "yesterday": total_day.get(yday, 0.0) * price},
        "first_used": used[0] if used else None, "last_used": used[-1] if used else None,
        "chart": chart, "periods": periods, "default_period": default,
        "sources": sources, "unavailable": sorted(k for k, v in sources.items() if v != "ok" and k != "functions"),
        "errors": rd.get("errors") or {},
    }


def report(session: Session, user_id: int, now: datetime | None = None) -> dict[str, Any]:
    conns = list(session.scalars(select(DbConnection).where(DbConnection.user_id == user_id, DbConnection.kind == "snowflake")))
    return {"accounts": [view(session, c, now) for c in conns], "generated_at": iso(now or utcnow())}


def explain(session: Session, user_id: int, now: datetime | None = None) -> str | None:
    """Plain words for "what does Cortex AI cost us?": the same figures as the page, from the bill, per account."""
    lines = []
    for v in report(session, user_id, now)["accounts"]:
        if not v["scanned_at"]:
            continue
        m, p = v["month"], v["periods"][v["default_period"]]
        span = {"30": "the last 30 days", "90": "the last 90 days", "365": "the last 12 months"}[v["default_period"]]
        if not p["credits"] and not p["tokens"]:
            lines.append(f"{v['name']}: no Cortex AI use in the last 12 months.")
            continue
        head = f"{v['name']}: this month so far ${m['mtd']:,.2f}" + (f" (last used {v['last_used']})" if not m["mtd"] and v["last_used"] else "") + "."
        parts = ", ".join(f"{x['name']} ${x['cost']:,.2f}" for x in p["products"] if x["has_credits"] and x["cost"] >= 0.005)
        who = ", ".join(u["name"] for u in p["users"][:3])
        lines.append(f"{head} In {span}: ${p['cost']:,.2f} ({p['credits']:,.2f} credits at ${v['price']:,.2f} each)"
                     + (f": {parts}" if parts else "") + "." + (f" Used by {who}." if who else ""))
    return "\n".join(lines) or None
