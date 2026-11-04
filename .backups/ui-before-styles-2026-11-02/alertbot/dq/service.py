"""Connections people own, the pipeline → table mappings they choose, and how a mapped table is measured.

A connection belongs to the person who entered it and stays connected (the encrypted credential is reused by every
scheduled check and the next time they sign in). A mapping says "pipeline P loads SCHEMA.TABLE on connection C" in a
project; mapping a table is what approves it (and only its own columns) for checking. Suggestions come from lineage:
the tables each pipeline writes, matched to the person's connections by server and database.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import BotState, DbConnection, DqMapping, Project, User
from ..timeutil import iso, utcnow
from . import sql as T
from .client import ConnInfo, DbClient, DbError, forget
from .privacy import SafeError, safe_name
from .vault import VaultError, seal, unseal

log = logging.getLogger(__name__)

COUNT_METRICS = {"rows", "null", "empty", "negative", "out_of_range", "invalid", "rule", "null_key"}
MIN_GROUP = 10            # statistics (averages, min/max, the commonest value's share) need at least this many rows
MAX_COLUMNS = 60
MAX_CATEGORIES = 6
NOT_CATEGORY = re.compile(r"(ID|KEY|_NO|_NUM|NUMBER|NAME|DESC|DESCRIPTION|COMMENT|REMARKS?|ADDRESS|EMAIL|PHONE|FILE|PATH|URL|HASH|"
                          r"TITLE|TEXT|BODY|CONTENT|NOTE|NOTES|MESSAGE|SUMMARY|LINK)$", re.I)
ID_LIKE = re.compile(r"(^ID$|_ID$|ID$|_KEY$|_NO$|_NUM$|NUMBER$|_SEQ$|_CODE$)", re.I)
LOAD_COL = re.compile(r"(LOAD|INSERT|INGEST|UPLOAD|CREATED|ETL|RECEIVED|BATCH|PUBLISH|MODIFIED|UPDATED)[A-Z_]*(DATE|TIME|TS|DT|AT|ON)$|"
                      r"^(LOAD|INSERT)_?(DATE|DT|TS)$", re.I)
SQL_PLATFORMS = {"SQL Server", "Azure SQL", "Azure SQL MI", "Synapse SQL"}


class PlanError(ValueError):
    """A mapping's settings name something the table doesn't have."""


# ── what a person can configure per mapped table (validated; never SQL) ─────────

_Name = str


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RangeSpec(_M):
    column: _Name
    min: float | None = None
    max: float | None = None


class TypedSpec(_M):
    column: _Name
    type: Literal["int", "decimal", "date"]


class RuleSpec(_M):
    name: str = Field(min_length=1, max_length=80)
    column: _Name
    op: Literal[">", ">=", "<", "<=", "=", "<>", "between", "in", "not_in"]
    values: list[float | int | str] = Field(min_length=1, max_length=50)
    severity: Literal["warning", "critical"] = "warning"
    max_fail_rate: float = Field(default=0.0, ge=0, le=1)

    @field_validator("name")
    @classmethod
    def _plain(cls, v: str) -> str:
        if re.search(r"[\x00-\x1f;'\"]", v):
            raise ValueError("a rule name is plain words")
        return v.strip()

    @model_validator(mode="after")
    def _arity(self) -> RuleSpec:
        if self.op == "between" and len(self.values) != 2:
            raise ValueError("between needs two values")
        if self.op not in ("between", "in", "not_in") and len(self.values) != 1:
            raise ValueError(f"{self.op} needs one value")
        return self

    def display(self) -> str:
        vals = ", ".join(str(v) for v in self.values)
        return {"between": f"{self.column} between {vals.replace(', ', ' and ')}", "in": f"{self.column} in ({vals})",
                "not_in": f"{self.column} not in ({vals})"}.get(self.op, f"{self.column} {self.op} {vals}")


class RefSpec(_M):
    column: _Name
    schema_name: _Name = Field(alias="schema")
    table: _Name
    ref_column: _Name

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MappingSettings(_M):
    key: list[_Name] = Field(default_factory=list)               # business key for duplicates
    date_column: _Name | None = None                              # "today's rows"; empty = found automatically
    required: list[_Name] = Field(default_factory=list)          # should (almost) never be empty
    sensitive: list[_Name] = Field(default_factory=list)         # only ever counted as empty / not empty
    stats: list[_Name] = Field(default_factory=list)             # approved for min / max / average / std. deviation
    distribution: list[_Name] | None = None                       # columns whose mix is watched; None = automatic
    ranges: list[RangeSpec] = Field(default_factory=list)
    typed: list[TypedSpec] = Field(default_factory=list)          # text columns that must read as int / decimal / date
    rules: list[RuleSpec] = Field(default_factory=list)
    references: list[RefSpec] = Field(default_factory=list)
    freshness_hours: float | None = Field(default=None, gt=0, le=24 * 60)
    every_hours: float = Field(default=6, ge=0.25, le=24 * 7)

    def columns_named(self) -> set[str]:
        names = set(self.key) | set(self.required) | set(self.sensitive) | set(self.stats) | set(self.distribution or [])
        names |= {r.column for r in self.ranges} | {t.column for t in self.typed} | {r.column for r in self.rules}
        names |= {r.column for r in self.references}
        if self.date_column:
            names.add(self.date_column)
        return names


# ── connections ───────────────────────────────────────────────────────────────


def info_of(c: DbConnection) -> ConnInfo:
    return ConnInfo(id=c.id, kind=c.kind, host=c.host, port=c.port, database=c.database, auth=c.auth, username=c.username,
                    secret=unseal(c.secret_enc), options=dict(c.options or {}), home_account_id=c.home_account_id,
                    version=iso(c.updated_at) or "")


def client_of(c: DbConnection) -> DbClient:
    return DbClient(info_of(c))


def canonical(c: DbConnection, schema: str, table: str) -> str:
    """How the rest of the bot names the table (lineage, incidents, load checks): DB.SCHEMA.TABLE in capitals."""
    return f"{c.database}.{schema}.{table}".upper()


def norm_host(kind: str, host: str | None) -> str:
    h = (host or "").strip().lower()
    h = re.sub(r"^(tcp:|https?://)", "", h)
    h = re.split(r"[,\\/]", h)[0]
    h = re.sub(r":\d+$", "", h).rstrip(".")
    if kind == "snowflake":
        h = h.removesuffix(".snowflakecomputing.com")
    return h


def connection_view(c: DbConnection, mappings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A connection as the portal sees it: everything except the secret."""
    return {"id": c.id, "name": c.name, "kind": c.kind, "host": c.host, "port": c.port, "database": c.database, "auth": c.auth,
            "username": c.username, "options": {k: v for k, v in (c.options or {}).items() if k in ("encrypt", "trust_server_certificate", "warehouse", "role")},
            "status": c.status, "can_write": c.can_write, "last_error": c.last_error, "last_ok_at": iso(c.last_ok_at),
            "last_used_at": iso(c.last_used_at), "created_at": iso(c.created_at), "mappings": mappings or []}


def test_connection(session: Session, c: DbConnection) -> dict[str, Any]:
    """Connect now and record the result on the connection (used on save, on Test, and when someone signs in)."""
    now = utcnow()
    try:
        r = client_of(c).test()
        c.status, c.last_error, c.last_ok_at, c.can_write = "ok", None, now, r.get("can_write")
    except (DbError, VaultError) as exc:
        c.status, c.last_error = "error", str(exc)
    except Exception as exc:  # noqa: BLE001
        from .privacy import sanitize_error

        c.status, c.last_error = "error", sanitize_error(exc).message
    c.last_used_at = now
    session.flush()
    return {"ok": c.status == "ok", "error": c.last_error, "can_write": c.can_write}


def reconnect_all(session: Session, user: User) -> list[dict[str, Any]]:
    """Signing in again reconnects every database the person connected before (in parallel, each time-limited)."""
    conns = list(session.scalars(select(DbConnection).where(DbConnection.user_id == user.id)))
    infos = []
    for c in conns:
        try:
            infos.append((c, client_of(c)))
        except VaultError as exc:
            c.status, c.last_error = "error", str(exc)

    def probe(cl: DbClient) -> tuple[bool, str | None, bool | None]:
        try:
            r = cl.test()
            return True, None, r.get("can_write")
        except DbError as exc:
            return False, str(exc), None
        except Exception as exc:  # noqa: BLE001
            from .privacy import sanitize_error

            return False, sanitize_error(exc).message, None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda pair: probe(pair[1]), infos))
    now = utcnow()
    for (c, _), (ok, err, can_write) in zip(infos, results, strict=True):
        c.status, c.last_error, c.last_used_at = ("ok" if ok else "error"), err, now
        if ok:
            c.last_ok_at, c.can_write = now, can_write
    session.flush()
    return [connection_view(c) for c in conns]


def save_connection(session: Session, user: User, data: dict[str, Any], existing: DbConnection | None = None) -> DbConnection:
    """Create or update a connection. A new secret replaces the old; an empty one keeps it."""
    c = existing or DbConnection(user_id=user.id)
    for k in ("name", "kind", "host", "port", "database", "auth", "username"):
        if k in data and data[k] is not None:
            setattr(c, k, data[k])
    if data.get("options") is not None:
        c.options = {k: v for k, v in data["options"].items() if k in ("encrypt", "trust_server_certificate", "warehouse", "role")}
    if data.get("secret"):
        c.secret_enc = seal(data["secret"])
    if c.auth == "entra":
        c.home_account_id = data.get("home_account_id") or c.home_account_id
    c.updated_at = utcnow()
    if existing is None:
        session.add(c)
    session.flush()
    forget(c.id)
    return c


# ── plans: what to measure on one mapped table ────────────────────────────────


def settings_of(m: DqMapping) -> MappingSettings:
    return MappingSettings.model_validate(m.settings or {})


def build_plan(client: DbClient, m: DqMapping, columns: list[tuple[str, str]] | None = None) -> T.Plan:
    """Decide the checks from the table's own metadata and the mapping's settings. Every name must be a real column."""
    d = client.dialect
    cols = columns if columns is not None else client.columns(m.schema_name, m.table_name)
    if not cols:
        raise DbError(SafeError("not_found", f"{m.schema_name}.{m.table_name} wasn't found, or this login can't see its columns."))
    kinds = {n: d.kind_of(t) for n, t in cols}
    s = settings_of(m)
    unknown = sorted(s.columns_named() - set(kinds))
    if unknown:
        raise PlanError(f"{m.schema_name}.{m.table_name} has no column {', '.join(unknown)}")
    names = [n for n, _ in cols]
    date = s.date_column or next((n for n in names if kinds[n] == "date" and LOAD_COL.search(n)), None) \
        or next((n for n in names if kinds[n] == "date"), None)
    sensitive = set(s.sensitive)
    watched = [n for n in names if n != date][:MAX_COLUMNS]
    numeric = [n for n in watched if kinds[n] == "number" and n not in sensitive]
    measures = [n for n in numeric if not ID_LIKE.search(n) and n not in s.key][:20]
    stats = [n for n in s.stats if n not in sensitive and kinds[n] == "number"]
    if s.distribution is not None:
        cats = [n for n in s.distribution if n not in sensitive and kinds[n] in ("text", "number")]
    else:
        cats = [n for n in watched if kinds[n] == "text" and n not in sensitive and not NOT_CATEGORY.search(n)][:MAX_CATEGORIES]
    for t in s.typed:
        if kinds[t.column] != "text":
            raise PlanError(f"{t.column} isn't a text column, so it can't hold badly typed values")
    refs = []
    for r in s.references:
        rcols = {n for n, _ in client.columns(r.schema_name, r.table)}
        if r.ref_column not in rcols:
            raise PlanError(f"{r.schema_name}.{r.table}.{r.ref_column} wasn't found")
        refs.append((r.column, r.schema_name, r.table, r.ref_column))
    rules = []
    for r in s.rules:
        if kinds[r.column] == "number" and any(isinstance(v, str) for v in r.values):
            try:
                vals = [float(v) for v in r.values]
            except ValueError as exc:
                raise PlanError(f"rule '{r.name}': {r.column} is a number column") from exc
        else:
            vals = list(r.values)
        rules.append((r.name, r.column, r.op, vals))
    return T.Plan(schema=safe_name(m.schema_name), table=safe_name(m.table_name), columns=kinds, date_column=date,
                  key=list(s.key), watched=watched, numeric=numeric, measures=measures, stats=stats, categories=cats,
                  ranges=[(r.column, r.min, r.max) for r in s.ranges if r.min is not None or r.max is not None],
                  typed=[(t.column, t.type) for t in s.typed], rules=rules, references=refs)


# ── one table's run: every query memoized, so each check tool reads the same scan ────


@dataclass
class TableRun:
    """The measurements of one mapped table in one run. Owned by that run only (no shared state between runs)."""

    client: DbClient
    plan: T.Plan
    start: datetime  # today's midnight (UTC, naive) in the bot's time zone
    _memo: dict[str, Any] = field(default_factory=dict)

    @property
    def params(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(sep=" ") if self.client.dialect.name == "sqlite" else self.start}

    def _once(self, key: str, fn: Any) -> Any:
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def rows_total(self) -> int:
        return self._once("rows_total", lambda: self.client.aggregate(T.rows_total(self.client.dialect, self.plan))["m0"] or 0)

    def scan(self) -> tuple[dict[str, Any], list[T.Part]]:
        def go() -> tuple[dict[str, Any], list[T.Part]]:
            plan = self.plan
            # a first pass counts the rows; statistics on fewer than MIN_GROUP rows could describe a single record
            q, parts = T.profile(self.client.dialect, T.Plan(schema=plan.schema, table=plan.table, columns=plan.columns,
                                                             date_column=plan.date_column), self.params)
            n = self.client.aggregate(q)["m0"] or 0
            if n < MIN_GROUP:
                plan = T.Plan(**{**plan.__dict__, "measures": [], "stats": [], "categories": []})
                self._memo["suppressed"] = True
            q, parts = T.profile(self.client.dialect, plan, self.params)
            return self.client.aggregate(q), parts
        return self._once("scan", go)

    def values(self, metric: str) -> dict[str, Any]:
        """One kind of measurement per column. Counts over zero rows come back from SQL as NULL: they're 0."""
        row, parts = self.scan()
        count = metric in COUNT_METRICS
        return {p.field: (row[f"m{i}"] or 0) if count else row[f"m{i}"] for i, p in enumerate(parts) if p.metric == metric}

    def rows_today(self) -> int:
        row, _ = self.scan()
        return row["m0"] or 0

    def latest(self) -> datetime | None:
        q = T.latest(self.client.dialect, self.plan)
        return self._once("latest", lambda: self.client.aggregate(q)["m0"] if q else None)

    def duplicates(self) -> tuple[int | None, int | None]:
        q = T.duplicates(self.client.dialect, self.plan, self.params)
        r = self._once("dups", lambda: self.client.aggregate(q) if q else None)
        return (r["m0"], r["m1"]) if r else (None, None)

    def shares(self) -> dict[str, float]:
        def go() -> dict[str, float]:
            n = self.rows_today()
            if n < MIN_GROUP or self._memo.get("suppressed"):
                return {}
            out = {}
            for c in self.plan.categories:
                r = self.client.aggregate(T.top_share(self.client.dialect, self.plan, c, self.params))
                if r["m0"] is not None:
                    out[c] = float(r["m0"]) / n
            return out
        return self._once("shares", go)

    def partitions(self) -> tuple[int | None, int | None]:
        """(days with rows in the last 7 complete days, days with rows in the 21 days before): missing loads show up
        as gaps in a table that normally gets rows every day."""
        q = T.partitions(self.client.dialect, self.plan)
        if q is None:
            return None, None

        def go() -> tuple[int | None, int | None]:
            marks = {"t0": self.start, "w7": self.start - timedelta(days=7), "w28": self.start - timedelta(days=28)}
            params = {k: (v.isoformat(sep=" ") if self.client.dialect.name == "sqlite" else v) for k, v in marks.items()}
            r = self.client.aggregate(T.Query(q.name, q.sql, params, q.spec))
            return r["m0"], r["m1"]
        return self._once("partitions", go)

    def orphans(self) -> dict[str, int]:
        def go() -> dict[str, int]:
            return {f"{c}→{rs}.{rt}.{rc}": int(self.client.aggregate(T.orphans(self.client.dialect, self.plan, (c, rs, rt, rc), self.params))["m0"] or 0)
                    for c, rs, rt, rc in self.plan.references}
        return self._once("orphans", go)


def as_utc(v: Any, tz: Any) -> datetime | None:
    if v is None:
        return None
    if not isinstance(v, datetime):
        v = datetime.combine(v, datetime.min.time())
    if v.tzinfo is None:
        v = v.replace(tzinfo=tz)
    return v.astimezone(UTC).replace(tzinfo=None)


def today_start(now: datetime, tz: Any) -> datetime:
    local = now.replace(tzinfo=UTC).astimezone(tz)
    midnight = datetime.combine(local.date(), datetime.min.time(), tzinfo=tz)
    return midnight.astimezone(UTC).replace(tzinfo=None)


# ── suggestions from lineage ─────────────────────────────────────────────────


def _matches(conn: DbConnection, target: dict[str, Any]) -> str | None:
    platform = target.get("platform") or ""
    if conn.kind == "sqlserver" and platform not in SQL_PLATFORMS:
        return None
    if conn.kind == "snowflake" and platform != "Snowflake":
        return None
    if conn.kind == "sqlite":
        return None
    obj = str(target.get("table") or "").upper().split(".")
    if len(obj) != 3 or obj[0] != conn.database.upper():
        return None
    host = norm_host(conn.kind, target.get("host"))
    if host and host == norm_host(conn.kind, conn.host):
        return "exact"
    return "database" if not host else None


def suggestions(session: Session, user: User, projects: list[Project]) -> list[dict[str, Any]]:
    """Tables the projects' pipelines write that sit on one of this person's databases and aren't mapped yet."""
    conns = list(session.scalars(select(DbConnection).where(DbConnection.user_id == user.id)))
    if not conns or not projects:
        return []
    mapped = {(m.project_id, m.pipeline, canonical(m.connection, m.schema_name, m.table_name))
              for m in session.scalars(select(DqMapping).where(DqMapping.project_id.in_([p.id for p in projects])))}
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for p in projects:
        inv = session.get(BotState, f"inventory:{p.key}")
        for item in ((inv.value or {}).get("items") or []) if inv else []:
            for step in item.get("lineage") or []:
                for target in step.get("writes") or []:
                    if target.get("kind") != "table" or target.get("dynamic"):
                        continue
                    for c in conns:
                        how = _matches(c, target)
                        if not how:
                            continue
                        obj = str(target["table"]).upper()
                        if (p.id, item["name"], obj) in mapped:
                            continue
                        _, schema, table = obj.split(".")
                        out.setdefault((p.id, item["name"], c.id, obj), {
                            "project": p.key, "project_name": p.display_name, "pipeline": item["name"], "connection_id": c.id,
                            "connection": c.name, "schema": schema, "table": table, "object": obj, "match": how})
    return sorted(out.values(), key=lambda s: (s["match"] != "exact", s["project_name"], s["pipeline"], s["object"]))


def resolve_table(client: DbClient, schema: str, table: str) -> tuple[str, str]:
    """The table's real spelling in the database (lineage names are in capitals)."""
    for t in client.tables():
        if t["schema"].lower() == schema.lower() and t["table"].lower() == table.lower():
            return t["schema"], t["table"]
    raise DbError(SafeError("not_found", f"{schema}.{table} wasn't found, or this login can't see it."))


def mapping_view(m: DqMapping) -> dict[str, Any]:
    c = m.connection
    return {"id": m.id, "pipeline": m.pipeline, "schema": m.schema_name, "table": m.table_name,
            "object": canonical(c, m.schema_name, m.table_name), "connection_id": c.id, "connection": c.name,
            "connection_kind": c.kind, "connection_status": c.status, "owner_id": c.user_id, "settings": m.settings or {},
            "automatic": m.created_by is None, "follow_azure": m.follow_azure is not False,
            "retired": m.retired_at is not None, "retired_at": iso(m.retired_at), "retired_reason": m.retired_reason, "retired_by": m.retired_by,
            "enabled": m.enabled, "created_at": iso(m.created_at), "updated_at": iso(m.updated_at)}


def mappings_of(session: Session, project: Project) -> list[DqMapping]:
    return list(session.scalars(select(DqMapping).where(DqMapping.project_id == project.id).order_by(DqMapping.pipeline, DqMapping.table_name)))


def checked(m: DqMapping) -> bool:
    """Is this mapping's table checked now: not paused by a person, and not retired because Azure changed."""
    return bool(m.enabled) and m.retired_at is None


def shown_mappings(maps: list[DqMapping]) -> list[DqMapping]:
    """For views keyed by table: the live mappings, plus a retired one only where no live mapping covers its table."""
    live = [m for m in maps if m.retired_at is None]
    objs = {canonical(m.connection, m.schema_name, m.table_name) for m in live}
    return live + [m for m in maps if m.retired_at is not None and canonical(m.connection, m.schema_name, m.table_name) not in objs]


def _loads(item: dict[str, Any], by_name: dict[str, dict[str, Any]], conn: DbConnection, seen: set[str] | None = None) -> tuple[set[str], bool]:
    """Tables on this connection's database the pipeline loads (itself, or through pipelines it runs), and whether it
    may load others its definition doesn't name (a table set at run time, a stored procedure, an unreadable definition)."""
    seen = seen if seen is not None else set()
    if item["name"] in seen:
        return set(), False
    seen.add(item["name"])
    if item.get("lineage") is None:
        return set(), True
    tables: set[str] = set()
    unknown = False
    for step in item["lineage"]:
        if step.get("type") == "Delete":
            continue
        for t in step.get("writes") or []:
            if t.get("kind") in ("database", "dataset") and _matches(conn, {**t, "table": f"{t.get('database') or conn.database}.X.X"}):
                unknown = True  # a stored procedure or an unnamed dataset on this database: it may write anything there
            elif t.get("kind") == "table" and t.get("dynamic"):
                unknown = True
            elif t.get("kind") == "table" and _matches(conn, t):
                tables.add(str(t["table"]).upper())
        child = by_name.get(step.get("runs") or "")
        if child is not None:
            more, unk = _loads(child, by_name, conn, seen)
            tables |= more
            unknown = unknown or unk
    return tables, unknown


def reconcile_mappings(session: Session, project: Project, items: list[dict[str, Any]], now: datetime) -> list[str]:
    """Keep "which pipeline loads which table" in step with Azure, by itself, whenever the bot re-reads the factory:
    a renamed pipeline's mapping moves to the pipeline that now loads the table (its settings kept); a mapping whose
    pipeline is gone, or no longer loads the table, is retired (kept and shown, not checked) and comes back by itself if
    the pipeline loads the table again. A person can say "keep checking it anyway" (follow_azure = False)."""
    if not items:
        return []
    by_name = {i["name"]: i for i in items}
    maps = mappings_of(session, project)
    notes: list[str] = []
    for m in maps:
        if m.retired_by == "database":
            continue  # the table itself is gone: only the database can bring it back (verify_tables)
        if m.follow_azure is False:
            if m.retired_at is not None:
                m.retired_at, m.retired_reason, m.retired_by = None, None, None
            continue
        obj = canonical(m.connection, m.schema_name, m.table_name)
        item = by_name.get(m.pipeline)
        reason = None
        if item is None:
            # renamed in Azure: the new name is new to the bot (no mappings yet), and it's the one pipeline loading this table
            known = {x.pipeline for x in maps if x is not m}
            heirs = [n for n, it in by_name.items() if n not in known and obj in _loads(it, by_name, m.connection)[0]]
            if len(heirs) == 1:
                old, m.pipeline = m.pipeline, heirs[0]
                m.retired_at, m.retired_reason, m.retired_by, m.updated_at = None, None, None, now
                notes.append(f"{old} → {m.pipeline}: {m.table_name} follows the renamed pipeline")
                log.info("data checks: %s was renamed to %s; its mapping to %s moved with it", old, m.pipeline, obj)
                continue
            reason = f"{m.pipeline} is no longer in Azure (renamed or deleted)."
        else:
            # only on evidence: the definition names the tables it loads on this database, and this isn't one of them
            # (a pipeline loading nothing visible here may do it through a notebook or a procedure: left alone)
            tables, unknown = _loads(item, by_name, m.connection)
            if tables and not unknown and obj not in tables:
                reason = f"{m.pipeline} no longer loads {m.table_name}; it now loads {', '.join(sorted(t.split('.')[-1] for t in tables))}."
        if reason and m.retired_at is None:
            m.retired_at, m.retired_reason, m.retired_by, m.updated_at = now, reason, "azure", now
            notes.append(reason)
            log.info("data checks: retired %s → %s: %s", m.pipeline, obj, reason)
        elif reason is None and m.retired_at is not None:
            m.retired_at, m.retired_reason, m.retired_by, m.updated_at = None, None, None, now
            notes.append(f"{m.pipeline} loads {m.table_name} again")
    session.flush()
    _close_unchecked(session, project, maps, now)
    return notes


_VERIFIED: dict[int, datetime] = {}  # connection id → when its tables were last re-read
VERIFY_EVERY = timedelta(minutes=10)


def verify_tables(session: Session, conn: DbConnection, now: datetime) -> dict[str, Any]:
    """Re-read the database: is it reachable, and does every table mapped on it still exist? A table that's gone
    (dropped, renamed, or this login lost access) retires its mappings, saying so; when it's back, they come back.
    Reads the catalogue only (names), never rows."""
    # 1. ask the database (network, maybe slow): nothing is written meanwhile, so nobody waits on the bot's own database
    maps = list(session.scalars(select(DqMapping).where(DqMapping.connection_id == conn.id)))
    have: set[tuple[str, str]] | None = None
    can_write = None
    try:
        client = client_of(conn)
        can_write = client.test().get("can_write")
        have = {(t["schema"].lower(), t["table"].lower()) for t in client.tables()}
        error = None
    except (DbError, VaultError) as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        from .privacy import sanitize_error

        error = sanitize_error(exc).message
    # 2. write what it said, in one quick step
    conn.last_used_at = now
    if error is None:
        conn.status, conn.last_error, conn.last_ok_at, conn.can_write = "ok", None, now, can_write
    else:
        conn.status, conn.last_error = "error", error
    out: dict[str, Any] = {"id": conn.id, "name": conn.name, "kind": conn.kind, "status": conn.status, "error": conn.last_error,
                           "tables": 0, "missing": [], "back": []}
    if have is None:
        session.flush()
        return out
    out["tables"] = len(have)
    for m in maps:
        here = (m.schema_name.lower(), m.table_name.lower()) in have
        name = f"{m.schema_name}.{m.table_name}"
        if not here and m.retired_by != "database":
            m.retired_at, m.retired_by, m.updated_at = now, "database", now
            m.retired_reason = f"{name} no longer exists in {conn.database} (dropped or renamed), or this login can't see it any more."
            out["missing"].append(name)
            log.info("data checks: %s is gone from %s; its mapping is retired", name, conn.name)
        elif here and m.retired_by == "database":
            m.retired_at, m.retired_reason, m.retired_by, m.updated_at = None, None, None, now
            out["back"].append(name)
    session.flush()
    for p in {m.project_id for m in maps}:
        project = session.get(Project, p)
        if project is not None:
            _close_unchecked(session, project, mappings_of(session, project), now)
    out["missing"], out["back"] = sorted(set(out["missing"])), sorted(set(out["back"]))
    return out


def _close_unchecked(session: Session, project: Project, maps: list[DqMapping], now: datetime) -> None:
    """A data-check incident whose table nothing checks any more (mapping retired, paused or removed) can't resolve by
    itself: close it, saying why. The register keeps it."""
    from ..db import Incident, IncidentEvent
    from ..engine.incidents import BOT

    live = {canonical(m.connection, m.schema_name, m.table_name) for m in maps if checked(m)}
    why_of = {canonical(m.connection, m.schema_name, m.table_name): m.retired_reason for m in maps if m.retired_reason}
    for inc in session.scalars(select(Incident).where(Incident.project_id == project.id, Incident.status.in_(("open", "acknowledged")),
                                                      Incident.scope.like("data:%"))):
        obj = inc.scope.removeprefix("data:")
        if obj in live:
            continue
        why = f"The bot no longer checks {obj.split('.')[-1]}" + (f": {why_of[obj]}" if obj in why_of else " (its mapping was paused or removed).")
        inc.status, inc.resolved_at, inc.updated_at = "resolved", now, now
        inc.resolved_by, inc.resolution = BOT, why
        session.add(IncidentEvent(incident=inc, kind="resolved", at=now, message=why, data={"by": BOT}))


# ── the cycle hook ────────────────────────────────────────────────────────────


SENSITIVE_NAME = re.compile(r"(EMAIL|E_?MAIL|PHONE|MOBILE|NAME|ADDRESS|STREET|POSTCODE|ZIP|SSN|NATIONAL|PASSPORT|DOB|BIRTH|SALARY|WAGE|"
                            r"IBAN|ACCOUNT_?NO|CARD|PASSWORD|TOKEN|SECRET|IP_?ADDR)", re.I)


def dismissed(session: Session, project_key: str) -> set[str]:
    row = session.get(BotState, f"dq-dismissed:{project_key}")
    return set((row.value or {}).get("keys", [])) if row else set()


def set_dismissed(session: Session, project_key: str, key: str, on: bool) -> None:
    """Someone removed an automatic mapping: never map it again by itself (until they map it themselves)."""
    row = session.get(BotState, f"dq-dismissed:{project_key}")
    if row is None:
        row = BotState(key=f"dq-dismissed:{project_key}", value={"keys": []})
        session.add(row)
    keys = set((row.value or {}).get("keys", []))
    keys = keys | {key} if on else keys - {key}
    row.value, row.updated_at = {"keys": sorted(keys)}, utcnow()
    session.flush()


def auto_map(session: Session, project: Project) -> list[DqMapping]:
    """Tables this project's pipelines load, on a database someone connected (same server and database, from lineage),
    are mapped by themselves with the standard checks, so no one has to map them; columns that look personal are marked
    sensitive. A removed mapping stays removed."""
    gone = dismissed(session, project.key)
    made: list[DqMapping] = []
    owners = session.scalars(select(User).join(DbConnection, DbConnection.user_id == User.id).distinct())
    for user in list(owners):
        for sg in suggestions(session, user, [project]):
            if sg["match"] != "exact" or f"{sg['pipeline']}|{sg['object']}" in gone:
                continue
            conn = session.get(DbConnection, sg["connection_id"])
            if conn is None or conn.status != "ok":
                continue
            try:
                client = client_of(conn)
                schema, table = resolve_table(client, sg["schema"], sg["table"])
                sensitive = [n for n, _ in client.columns(schema, table) if SENSITIVE_NAME.search(n)]
            except (DbError, VaultError):
                continue
            m = DqMapping(project_id=project.id, connection_id=conn.id, pipeline=sg["pipeline"], schema_name=schema, table_name=table,
                          settings={"sensitive": sensitive} if sensitive else {}, created_by=None)
            m.connection = conn
            session.add(m)
            session.commit()  # before the next lookup in the warehouse: no write is held across network calls
            made.append(m)
            log.info("data checks: mapped %s → %s.%s automatically (lineage)", sg["pipeline"], schema, table)
    return made


def has_mappings(session: Session, project_id: int) -> bool:
    return session.scalar(select(DqMapping.id).where(DqMapping.project_id == project_id, DqMapping.enabled.is_(True),
                                                     DqMapping.retired_at.is_(None)).limit(1)) is not None


def last_load(session: Session, project_id: int, pipeline: str) -> datetime | None:
    """When the pipeline last finished a successful run (that's a new load of the tables it's mapped to)."""
    from ..db import Run

    return session.scalar(select(func.max(func.coalesce(Run.ended_at, Run.requested_at))).where(
        Run.project_id == project_id, or_(Run.pipeline == pipeline, Run.site == pipeline), Run.run_status == "Succeeded"))


def due_reason(ctx: Any, m: DqMapping) -> str | None:
    """Why a mapped table should be checked now, or None. Checked automatically: right after it's mapped, as soon as
    its pipeline finishes a new successful load, and at least every `every_hours`."""
    from ..db import MetricObservation

    obj = canonical(m.connection, m.schema_name, m.table_name)
    last_dq = ctx.session.scalar(select(func.max(MetricObservation.observed_at)).where(
        MetricObservation.project_id == ctx.project.id, MetricObservation.output_id == f"dq:{obj}"))
    if last_dq is None:
        return "first check"
    loaded = last_load(ctx.session, ctx.project.id, m.pipeline)
    if loaded is not None and loaded > last_dq:
        return f"{m.pipeline} loaded new data"
    table_load = ctx.session.scalar(select(func.max(MetricObservation.observed_at)).where(
        MetricObservation.project_id == ctx.project.id, MetricObservation.output_id == f"table:{obj}"))
    if table_load is not None and table_load > last_dq:
        return "a new load of the table"
    if m.updated_at and m.updated_at > last_dq:
        return "its checks were changed"
    if ctx.now - last_dq >= timedelta(hours=settings_of(m).every_hours):
        return "routine re-check"
    return None


def set_live(project_key: str, **value: Any) -> None:
    """What the data checks are doing right now (the portal shows "Checking … now"). Own short transaction."""
    from ..db import session_scope

    try:
        with session_scope() as s:
            row = s.get(BotState, f"dq-live:{project_key}") or BotState(key=f"dq-live:{project_key}")
            row.value, row.updated_at = {**value, "at": iso(utcnow())}, utcnow()
            s.add(row)
    except Exception:  # noqa: BLE001 - a status line must never stop a check
        log.debug("couldn't record the data-check status", exc_info=True)


UNREACHABLE = ("can't be reached", "refused the sign-in", "doesn't exist, or this login", "certificate", "multi-factor",
               "busy or restarting", "can't be decrypted", "ODBC driver")


def collect_mapped(ctx: Any, force: bool = False) -> Any:
    """The data checks of a project's mapped tables, read before the cycle's transaction (the LangGraph workflow's
    collect stage). Returns a QualityBatch for engine.quality.judge_quality, or None when nothing is mapped."""
    from ..engine.quality import QualityBatch
    from .graph import run_dq

    for conn in {m.connection for m in mappings_of(ctx.session, ctx.project)}:  # the databases themselves, every 10 minutes
        if ctx.now - _VERIFIED.get(conn.id, datetime.min) >= VERIFY_EVERY:
            _VERIFIED[conn.id] = ctx.now
            try:
                verify_tables(ctx.session, conn, ctx.now)
                ctx.session.commit()  # never hold a write while the warehouse is queried
            except Exception:  # noqa: BLE001 - a catalogue read failing never stops the checks
                ctx.session.rollback()
                log.warning("couldn't re-read the tables of %s", conn.name, exc_info=True)
    inv = ctx.session.get(BotState, f"inventory:{ctx.key}")
    if inv is not None and not (inv.value or {}).get("error"):  # Azure's current list: follow renames and changed pipelines
        reconcile_mappings(ctx.session, ctx.project, (inv.value or {}).get("items") or [], ctx.now)
        ctx.session.commit()
    try:
        auto_map(ctx.session, ctx.project)  # new tables the pipelines load on a connected database: map them by themselves
        ctx.session.commit()
    except Exception:  # noqa: BLE001 - mapping automatically is a bonus; the checks of existing mappings still run
        ctx.session.rollback()
        log.warning("automatic data-check mapping failed for %s", ctx.key, exc_info=True)
    maps = [m for m in mappings_of(ctx.session, ctx.project) if checked(m)]
    if not maps:
        return None
    reasons = {m.id: ("checked on request" if force else due_reason(ctx, m)) for m in maps}
    due = [m for m in maps if reasons[m.id]]
    if not due:
        return QualityBatch(targets=len(maps))
    names = [f"{m.schema_name}.{m.table_name}" for m in due]
    set_live(ctx.key, state="checking", tables=names, reasons=[reasons[m.id] for m in due])
    try:
        state = run_dq(ctx, [m.id for m in due], stage="collect")
    except Exception:
        set_live(ctx.key, state="idle")
        raise
    batch = QualityBatch(targets=len(maps), items=state.get("profiled", []), errors=state.get("errors", []), mapped_ids=[m.id for m in due])
    for t in (state.get("tables") or {}).values():
        if t.get("status") == "checked":
            batch.availability.append((t["object"], t["pipeline"], True, ""))
        elif t.get("status") == "error" and any(k in (t.get("error") or "") for k in UNREACHABLE):
            batch.availability.append((t["object"], t["pipeline"], False, t["error"]))
    if not batch.items and batch.errors and all(any(k in e for k in UNREACHABLE) for e in batch.errors):
        batch.unavailable = batch.errors[0]
    set_live(ctx.key, state="analysing" if batch.items else "idle", tables=names)
    return batch


def since(minutes: int) -> datetime:
    return utcnow() - timedelta(minutes=minutes)
