"""Data quality inside the tables the pipelines load (Snowflake), judged against each table's own history.

For every table a pipeline writes (found automatically from the Data Factory datasets) the bot profiles the newest data
once after each new load:

    row count        rows that arrived today (by the table's load-date column), against this weekday's normal
    null %           per column, against that column's normal (a column that's suddenly 15% empty is flagged)
    duplicates       repeated rows (or repeated keys when a key is configured)
    freshness        how old the newest row is
    schema           columns added / removed / retyped since the accepted baseline
    value ranges     negative values in a numeric column that has never had any ("revenue suddenly negative")
    business rules   SQL conditions you configure, e.g. order_amount > 0
    referential      values that must exist in another table, e.g. every BA_ID in CGO_MANIFEST

No thresholds to set by hand: each measure learns its normal range (the same robust baselines as everything else).
Results land on the table's data-quality scope (data:DB.SCHEMA.TABLE): one incident per table for its data, grouping
every check that fails on it. (Quality runs after each load, so it has its own scope; the load checks keep table:.) Read-only: SELECTs on INFORMATION_SCHEMA and the tables, nothing else.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select

from ..config import QualityConfig, QualityTable
from ..db import BotState, MetricObservation, SchemaSnapshot
from ..timeutil import iso, local_weekday
from .baselines import band_for
from .checks import Outcome, diff_schema, schema_fingerprint
from .context import ProjectContext

log = logging.getLogger(__name__)

DQ_PREFIX = "dq:"
DATE_TYPES = ("DATE", "TIMESTAMP", "DATETIME", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ")
NUM_TYPES = ("NUMBER", "DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT", "SMALLINT", "FLOAT", "DOUBLE", "REAL")
TEXT_TYPES = ("VARCHAR", "TEXT", "STRING", "CHAR", "NVARCHAR", "NCHAR")
# free text and identifiers have no meaningful "mix"; categories (type, status, country, product...) do
NOT_CATEGORY = re.compile(r"(ID|KEY|_NO|_NUM|NUMBER|NAME|DESC|DESCRIPTION|COMMENT|REMARKS?|ADDRESS|EMAIL|PHONE|FILE|PATH|URL|HASH)$", re.I)
MAX_DISTRIBUTION_COLUMNS = 6
ID_LIKE = re.compile(r"(^ID$|_ID$|ID$|_KEY$|_NO$|_NUM$|NUMBER$|_SEQ$|_CODE$)", re.I)  # numbers that are names, not amounts
LOAD_COL = re.compile(r"(LOAD|INSERT|INGEST|UPLOAD|CREATED|ETL|RECEIVED|BATCH)[A-Z_]*(DATE|TIME|TS|DT|AT)$|^(LOAD|INSERT)_?(DATE|DT|TS)$", re.I)


class QualityUnavailable(RuntimeError):
    """The warehouse can't be reached / signed in to (yet)."""


def _q(name: str) -> str:
    """A quoted identifier, exactly as the warehouse stores it (quotes inside are escaped, so it can't break out)."""
    return '"' + name.replace('"', '""') + '"'


def split_table(table: str) -> tuple[str | None, str, str]:
    parts = table.split(".")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    raise ValueError(f"table must be SCHEMA.TABLE or DB.SCHEMA.TABLE: {table}")


@dataclass
class Column:
    name: str
    type: str


@dataclass
class Profile:
    table: str
    columns: list[Column]
    date_column: str | None
    rows_total: int
    rows_today: int | None
    latest: datetime | None
    duplicate_rows: int | None
    nulls: dict[str, int] = field(default_factory=dict)       # column → empty values in the profiled rows
    negatives: dict[str, int] = field(default_factory=dict)   # numeric column → negative values
    rule_fails: dict[str, int] = field(default_factory=dict)  # rule name → rows breaking it
    orphans: dict[str, int] = field(default_factory=dict)     # "COL→TABLE.COL" → values with no match
    means: dict[str, float] = field(default_factory=dict)     # numeric column → average today
    top_share: dict[str, float] = field(default_factory=dict)  # column → share of rows its most common value has (never the value)
    profiled_rows: int = 0
    empties: dict[str, int] = field(default_factory=dict)       # text column → '' values (counted in nulls too)
    invalid: dict[str, int] = field(default_factory=dict)       # text column → values that don't read as their type
    out_of_range: dict[str, int] = field(default_factory=dict)  # column → values outside the approved range
    stats: dict[str, dict[str, float | None]] = field(default_factory=dict)  # approved column → min / max / avg / stddev
    duplicate_groups: int | None = None                         # keys that repeat
    null_key_rows: int | None = None                            # rows with an empty part of the business key
    days_recent: int | None = None                              # days with rows in the last 7 complete days
    days_before: int | None = None                              # … and in the 21 days before them


# ── the warehouse connection ──────────────────────────────────────────────────


@lru_cache(maxsize=4)
def _engine(dsn: str) -> sa.Engine:
    cfg = json.loads(dsn)
    if cfg["type"] == "sqlite":  # local stand-in: each schema is an attached SQLite file next to the main one
        from sqlalchemy import event

        engine = sa.create_engine(cfg["url"], connect_args={"check_same_thread": False})
        base = cfg["url"].split("///", 1)[-1]

        @event.listens_for(engine, "connect")
        def _attach(dbapi_conn, _rec):  # noqa: ANN001
            for schema in cfg.get("schemas", []):
                dbapi_conn.execute(f"ATTACH DATABASE '{base}.{schema.lower()}' AS {schema}")

        return engine
    from ..config import SourceConfig
    from ..connectors.feeds import _snowflake_engine

    src = SourceConfig(id="quality", type="snowflake", account=cfg["account"], user=cfg["user"], role=cfg.get("role", ""),
                       warehouse=cfg.get("warehouse", ""), database=cfg.get("database") or "SNOWFLAKE",
                       authenticator=cfg.get("authenticator", "externalbrowser"), private_key=cfg.get("private_key", ""))
    return _snowflake_engine(src)


class Warehouse:
    def __init__(self, q: QualityConfig, schemas: list[str] | None = None):
        self.q = q
        src = q.source
        self.dialect = src.type
        dsn = {"type": src.type, "url": src.url, "account": src.account, "user": src.user, "role": src.role,
               "warehouse": src.warehouse, "database": src.database, "authenticator": src.authenticator,
               "private_key": src.private_key, "schemas": sorted(set(schemas or []))}
        try:
            self.engine = _engine(json.dumps(dsn, sort_keys=True))
            with self.engine.connect() as c:
                c.execute(sa.text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            _engine.cache_clear()
            raise QualityUnavailable(f"can't connect to {src.type}: {str(exc)[:300]}") from exc

    def _fq(self, table: str) -> str:
        db, schema, name = split_table(table)
        if self.dialect == "sqlite":
            return f"{_q(schema)}.{_q(name)}"
        return ".".join(_q(p) for p in (db, schema, name) if p)

    def columns(self, table: str) -> list[Column]:
        db, schema, name = split_table(table)
        with self.engine.connect() as c:
            if self.dialect == "sqlite":
                rows = c.execute(sa.text(f"PRAGMA {_q(schema)}.table_info({_q(name)})")).all()
                return [Column(r[1].upper(), (r[2] or "TEXT").upper()) for r in rows]
            info = f"{_q(db)}.INFORMATION_SCHEMA.COLUMNS" if db else "INFORMATION_SCHEMA.COLUMNS"
            rows = c.execute(sa.text(f"SELECT COLUMN_NAME, DATA_TYPE FROM {info} WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
                                     "ORDER BY ORDINAL_POSITION"), {"s": schema.upper(), "t": name.upper()}).all()
            return [Column(r[0].upper(), str(r[1]).upper()) for r in rows]

    def profile(self, t: QualityTable, cols: list[Column], now: datetime, tz: Any, max_columns: int) -> Profile:
        fq = self._fq(t.table)
        names = {c.name for c in cols}
        date_col = (t.date_column or "").upper() or pick_date_column(cols)
        if date_col and date_col not in names:
            raise ValueError(f"{t.table}: date column {date_col} doesn't exist")
        midnight = datetime.combine(now.replace(tzinfo=UTC).astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
        start = midnight.astimezone(UTC).replace(tzinfo=None)
        where = f"WHERE {_q(date_col)} >= :start" if date_col else ""
        params: dict[str, Any] = {"start": start.isoformat(sep=" ") if self.dialect == "sqlite" else start}
        watched = [c for c in cols if c.name != date_col][:max_columns]
        parts = ["COUNT(*)"]
        parts += [f"SUM(CASE WHEN {_q(c.name)} IS NULL OR CAST({_q(c.name)} AS VARCHAR) = '' THEN 1 ELSE 0 END)" for c in watched]
        nums = [c for c in watched if c.type.split("(")[0] in NUM_TYPES]
        parts += [f"SUM(CASE WHEN {_q(c.name)} < 0 THEN 1 ELSE 0 END)" for c in nums]
        rules = [r for r in t.rules]
        parts += [f"SUM(CASE WHEN NOT ({r.sql}) THEN 1 ELSE 0 END)" for r in rules]  # rules are owner-configured SQL
        measured = [c for c in nums if not ID_LIKE.search(c.name) and c.name not in {k.upper() for k in t.key}]
        parts += [f"AVG(CAST({_q(c.name)} AS DOUBLE PRECISION))" for c in measured]
        cats = category_columns(t, watched)
        with self.engine.connect() as c:
            total = int(c.execute(sa.text(f"SELECT COUNT(*) FROM {fq}")).scalar() or 0)
            row = c.execute(sa.text(f"SELECT {', '.join(parts)} FROM {fq} {where}"), params).one()
            latest = c.execute(sa.text(f"SELECT MAX({_q(date_col)}) FROM {fq}")).scalar() if date_col else None
            dup = None
            if t.key:
                key = ", ".join(_q(k.upper()) for k in t.key)
                dup = c.execute(sa.text(f"SELECT COALESCE(SUM(n - 1), 0) FROM (SELECT COUNT(*) AS n FROM {fq} {where} GROUP BY {key}) g"),
                                params).scalar()
            elif self.dialect == "snowflake":
                dup = c.execute(sa.text(f"SELECT COUNT(*) - COUNT(DISTINCT HASH(*)) FROM {fq} {where}"), params).scalar()
            top_count: dict[str, float] = {}
            for col in cats:  # the commonest value's COUNT only: the value itself never leaves the warehouse
                top = c.execute(sa.text(f"SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM {fq} {where} GROUP BY {_q(col)}) g"), params).scalar()
                if top is not None:
                    top_count[col] = float(top)
            orphans: dict[str, int] = {}
            for ref in t.references:
                col, rcol = _q(ref.column.upper()), _q(ref.ref_column.upper())
                conds = [f"a.{col} IS NOT NULL", f"NOT EXISTS (SELECT 1 FROM {self._fq(ref.table)} b WHERE b.{rcol} = a.{col})"]
                if date_col:
                    conds.insert(0, f"a.{_q(date_col)} >= :start")
                n = c.execute(sa.text(f"SELECT COUNT(*) FROM {fq} a WHERE {' AND '.join(conds)}"), params).scalar()
                orphans[f"{ref.column.upper()}→{ref.table}.{ref.ref_column.upper()}"] = int(n or 0)
        profiled = int(row[0] or 0)
        i = 1
        nulls = {c.name: int(row[i + k] or 0) for k, c in enumerate(watched)}
        i += len(watched)
        negatives = {c.name: int(row[i + k] or 0) for k, c in enumerate(nums)}
        i += len(nums)
        rule_fails = {r.name: int(row[i + k] or 0) for k, r in enumerate(rules)}
        i += len(rules)
        means = {c.name: float(row[i + k]) for k, c in enumerate(measured) if row[i + k] is not None}
        top_share = {col: n / profiled for col, n in top_count.items()} if profiled >= 10 else {}
        return Profile(table=t.table, columns=cols, date_column=date_col, rows_total=total,
                       rows_today=profiled if date_col else None, latest=_as_utc(latest, tz), duplicate_rows=int(dup) if dup is not None else None,
                       nulls=nulls, negatives=negatives, rule_fails=rule_fails, orphans=orphans, profiled_rows=profiled,
                       means=means, top_share=top_share)


def _as_utc(v: Any, tz: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return None
    if not isinstance(v, datetime):
        v = datetime.combine(v, datetime.min.time())
    if v.tzinfo is None:
        v = v.replace(tzinfo=tz)
    return v.astimezone(UTC).replace(tzinfo=None)


def category_columns(t: QualityTable, cols: list[Column]) -> list[str]:
    """Text columns whose mix of values is worth watching: configured ones, else categories picked by name."""
    names = {c.name for c in cols}
    if t.distribution is not None:
        return [c.upper() for c in t.distribution if c.upper() in names]
    return [c.name for c in cols if c.type.split("(")[0] in TEXT_TYPES and not NOT_CATEGORY.search(c.name)][:MAX_DISTRIBUTION_COLUMNS]


def pick_date_column(cols: list[Column]) -> str | None:
    """The column that says when a row was loaded: a load/insert/upload date first, else the first date column."""
    dated = [c for c in cols if c.type.split("(")[0] in DATE_TYPES]
    for c in dated + cols:
        if LOAD_COL.search(c.name):
            return c.name
    return dated[0].name if dated else None


# ── checks ────────────────────────────────────────────────────────────────────


def quality_targets(ctx: ProjectContext) -> list[tuple[QualityTable, str]]:
    """(table settings, pipeline) for every table to check: configured ones, plus every Snowflake table the
    pipelines load when `tables: auto`, limited to the configured environments."""
    q = ctx.cfg.quality
    assert q is not None
    out: dict[str, tuple[QualityTable, str]] = {}
    if q.auto:
        adf = ctx.connectors.pipeline
        from .adf import _adf

        for pipeline in _adf(ctx).watched_pipelines():
            try:
                targets = adf.step_targets(pipeline)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            for t in targets.values():
                if t.get("kind") == "table" and (not q.environments or t.get("env") in q.environments):
                    out.setdefault(t["table"], (q.table(t["table"]), pipeline))
    for t in q.tables:
        out[t.table.upper()] = (t, out.get(t.table.upper(), (t, t.pipeline or "quality"))[1])
    return list(out.values())


RETRY_AFTER_FAILURE = timedelta(minutes=30)  # don't knock on a warehouse we can't sign in to every cycle


@dataclass
class Profiled:
    table: QualityTable
    pipeline: str
    columns: list[Column]
    profile: Profile | None


@dataclass
class QualityBatch:
    """What one round of data checks read from the warehouse (read-only; judged later inside the cycle)."""

    targets: int
    items: list[Profiled] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unavailable: str | None = None
    mapped_ids: list[int] = field(default_factory=list)  # mapped tables checked this round (they get an AI explanation after)
    availability: list[tuple[str, str, bool, str]] = field(default_factory=list)  # (table, pipeline, reachable, message)


def collect_quality(ctx: ProjectContext, force: bool = False) -> QualityBatch | None:
    """The slow part: find the tables, connect, profile each table that's due. Writes nothing to the bot's database."""
    q = ctx.cfg.quality
    if q is None or not q.enabled:
        return None
    targets = quality_targets(ctx)
    if not targets:
        return None
    status = get_status(ctx.session, ctx.key) or {}
    if not force and status.get("unavailable") and status.get("at"):
        since = ctx.now - datetime.fromisoformat(status["at"]).replace(tzinfo=None)
        if since < RETRY_AFTER_FAILURE:
            return QualityBatch(targets=len(targets), unavailable=status.get("message") or "unavailable")
    due = [(t, p) for t, p in targets if force or _due(ctx, t.table, q.every_hours)]
    if not due:
        return QualityBatch(targets=len(targets))
    schemas = sorted({split_table(t.table)[1] for t, _ in targets} | {split_table(r.table)[1] for t, _ in targets for r in t.references})
    try:
        wh = Warehouse(q, schemas)
    except QualityUnavailable as exc:
        return QualityBatch(targets=len(targets), unavailable=str(exc))
    batch = QualityBatch(targets=len(targets))
    for t, pipeline in due:
        try:
            cols = wh.columns(t.table)
            batch.items.append(Profiled(t, pipeline, cols, wh.profile(t, cols, ctx.now, ctx.settings.tz, q.max_columns) if cols else None))
        except Exception as exc:  # noqa: BLE001 - one unreadable table must not stop the others
            log.warning("quality check of %s failed: %s", t.table, exc)
            batch.errors.append(f"{t.table}: {str(exc)[:160]}")
    return batch


def judge_quality(ctx: ProjectContext, batch: QualityBatch | None) -> list[Outcome]:
    """The quick part, inside the cycle: compare each profile with its table's normal, remember it, record the status."""
    if batch is None:
        return []
    if batch.unavailable:
        set_status(ctx, ok=False, unavailable=True, message=batch.unavailable, tables=batch.targets)
        return []
    out: list[Outcome] = []
    for item in batch.items:
        out.extend(judge_table(ctx, item))
    for table, pipeline, ok, message in batch.availability:  # could the bot read the table at all?
        out.append(Outcome("source_available", f"data:{table}", "pass" if ok else "fail", None if ok else "warning",
                           "" if ok else f"{table}: couldn't be checked: {message}", site=pipeline))
    if batch.items or batch.errors:
        set_status(ctx, ok=not batch.errors or bool(batch.items), unavailable=False, tables=batch.targets,
                   checked=len(batch.items), failed_tables=len(batch.errors),
                   message="; ".join(batch.errors[:3]) if batch.errors else f"Checked {len(batch.items)} table(s).")
    return out


def run_quality(ctx: ProjectContext, force: bool = False) -> list[Outcome]:
    """Collect and judge in one go (CLI and tests; the cycle collects before its transaction)."""
    return judge_quality(ctx, collect_quality(ctx, force))


def set_status(ctx: ProjectContext, **value: Any) -> None:
    key = f"quality:{ctx.key}"
    row = ctx.session.get(BotState, key)
    if row is None:
        row = BotState(key=key)
        ctx.session.add(row)
    prev = row.value or {}
    row.value = {**prev, **value, "at": iso(ctx.now), **({"last_success_at": iso(ctx.now)} if value.get("checked") else {})}
    ctx.session.flush()
    row.updated_at = ctx.now


def accept_schema(ctx: ProjectContext, table: str) -> bool:
    """Owner action: the table's columns as last seen become its accepted schema."""
    from ..db import CheckResult

    last = ctx.session.scalars(select(CheckResult).where(
        CheckResult.project_id == ctx.project.id, CheckResult.scope == f"data:{table}", CheckResult.check == "schema_drift",
        CheckResult.status == "fail").order_by(CheckResult.created_at.desc(), CheckResult.id.desc()).limit(1)).first()
    current = (last.details or {}).get("current") if last else None
    if not current:
        return False
    ctx.session.add(SchemaSnapshot(project_id=ctx.project.id, output_id=f"{DQ_PREFIX}{table}", columns=current,
                                   fingerprint=schema_fingerprint(current), captured_at=ctx.now))
    ctx.session.flush()
    return True


def get_status(session: Any, project_key: str) -> dict[str, Any] | None:
    row = session.get(BotState, f"quality:{project_key}")
    return row.value if row else None


def _due(ctx: ProjectContext, table: str, every_hours: float) -> bool:
    """Check after each new load of the table (from ADF), and at least every `every_hours`."""
    last_dq = ctx.session.scalar(select(sa.func.max(MetricObservation.observed_at)).where(
        MetricObservation.project_id == ctx.project.id, MetricObservation.output_id == f"{DQ_PREFIX}{table}"))
    if last_dq is None:
        return True
    last_load = ctx.session.scalar(select(sa.func.max(MetricObservation.observed_at)).where(
        MetricObservation.project_id == ctx.project.id, MetricObservation.output_id == f"table:{table}"))
    return (last_load is not None and last_load > last_dq) or ctx.now - last_dq >= timedelta(hours=every_hours)


def judge_table(ctx: ProjectContext, item: Profiled) -> list[Outcome]:
    t, pipeline, cols, p = item.table, item.pipeline, item.columns, item.profile
    tz = ctx.settings.tz
    if p is None:
        return [Outcome("schema_drift", f"data:{t.table}", "skip", site=pipeline, message=f"{t.table} not found or not readable")]
    oid = f"{DQ_PREFIX}{t.table}"
    scope = f"data:{t.table}"
    ref = f"dq@{ctx.now:%Y%m%dT%H%M}"
    weekday = local_weekday(ctx.now, tz)
    base = p.rows_today if p.rows_today is not None else p.rows_total
    n = max(1, p.profiled_rows)
    values: list[tuple[str, str, float]] = [("row_count", "", float(base)), ("rows_total", "", float(p.rows_total))]
    for counts in (p.nulls, p.negatives, p.rule_fails, p.orphans, p.empties, p.invalid, p.out_of_range):  # NULL counts are 0
        for k in counts:
            counts[k] = counts[k] or 0
    values += [("null_rate", c, v / n) for c, v in p.nulls.items()] if p.profiled_rows else []
    values += [("negative_rate", c, v / n) for c, v in p.negatives.items()] if p.profiled_rows else []
    values += [("rule_fail_rate", r, v / n) for r, v in p.rule_fails.items()] if p.profiled_rows else []
    values += [("orphan_rate", k, v / n) for k, v in p.orphans.items()] if p.profiled_rows else []
    if p.duplicate_rows is not None and p.profiled_rows:
        values.append(("duplicate_rate", "", p.duplicate_rows / n))
    values += [("mean", c, v) for c, v in p.means.items()]
    values += [("top_share", c, share) for c, share in p.top_share.items()]
    values += [("empty_rate", c, v / n) for c, v in p.empties.items()] if p.profiled_rows else []
    values += [("invalid_rate", c, v / n) for c, v in p.invalid.items()] if p.profiled_rows else []
    values += [("out_of_range_rate", c, v / n) for c, v in p.out_of_range.items()] if p.profiled_rows else []
    values += [(m, c, float(v)) for c, st in p.stats.items() for m, v in st.items() if isinstance(v, (int, float))]
    values += [(m, "", float(v)) for m, v in (("profiled_rows", p.profiled_rows), ("duplicate_rows", p.duplicate_rows),
                                              ("duplicate_groups", p.duplicate_groups), ("null_key_rows", p.null_key_rows),
                                              ("days_recent", p.days_recent), ("days_before", p.days_before)) if v is not None]
    if p.latest is not None:
        values.append(("freshness_hours", "", max(0.0, (ctx.now - p.latest).total_seconds() / 3600)))

    def band(metric: str, fld: str = "") -> Any:
        return band_for(ctx, oid, pipeline, metric, fld, weekday, before=ctx.now, persist=False)

    info = {"table": t.table, "pipeline": pipeline, "date_column": p.date_column, "profiled_rows": p.profiled_rows,
            "rows_total": p.rows_total, "latest_row_at": iso(p.latest)}
    out: list[Outcome] = []
    what = "today" if p.rows_today is not None else "in total"

    # row count
    rb = band("row_count")
    if rb is None:
        out.append(Outcome("row_count", scope, "skip", site=pipeline, actual=base, message=f"{t.table}: learning its normal row count."))
    elif base < rb.low or base > rb.high:
        low = base < rb.low
        dev = (base / rb.median - 1) if rb.median else -1.0
        sev = "critical" if low and dev <= -0.5 else "warning"
        out.append(Outcome("row_count", scope, "fail", sev,
                           f"{t.table} has {base:,.0f} rows {what}; normally about {rb.median:,.0f} ({dev:+.0%}).",
                           base, rb.low, rb.high, ref, pipeline, {**info, "direction": "low" if low else "high", "deviation": dev}))
    else:
        out.append(Outcome("row_count", scope, "pass", site=pipeline, actual=base, expected_low=rb.low, expected_high=rb.high))

    # null % per column
    spikes = []
    for col, v in p.nulls.items():
        rate = v / n
        b = band("null_rate", col)
        limit = max(b.high if b else 0.05, (b.median if b else 0) + t.null_tolerance)
        if p.profiled_rows and rate > limit and rate >= 0.01 and (col in t.required or b is not None):
            spikes.append({"field": col, "rate": rate, "normal": b.median if b else None, "limit": limit})
        elif p.profiled_rows and col in t.required and v > 0 and t.required_strict:
            spikes.append({"field": col, "rate": rate, "normal": 0.0, "limit": 0.0})
    if spikes:
        worst = max(spikes, key=lambda s: s["rate"])
        sev = "critical" if worst["rate"] >= 0.15 else "warning"
        msg = "; ".join(f"{s['field']} is empty in {s['rate']:.1%} of rows (normally {s['normal']:.1%})" if s["normal"] is not None
                        else f"{s['field']} is empty in {s['rate']:.1%} of rows" for s in sorted(spikes, key=lambda s: -s["rate"])[:5])
        out.append(Outcome("null_spike", scope, "fail", sev, f"{t.table}: {msg}.", worst["rate"], None, worst["limit"], ref, pipeline,
                           {**info, "fields": spikes}))
    elif p.profiled_rows:
        out.append(Outcome("null_spike", scope, "pass", site=pipeline))

    # duplicates
    if p.duplicate_rows is not None and p.profiled_rows:
        rate = p.duplicate_rows / n
        b = band("duplicate_rate")
        limit = max(t.max_duplicate_rate, b.high if b else 0.0)
        if rate > limit:
            groups = f" across {p.duplicate_groups:,} repeated key(s)" if p.duplicate_groups else ""
            out.append(Outcome("duplicates", scope, "fail", "critical" if rate >= 0.1 else "warning",
                               f"{t.table}: {p.duplicate_rows:,} duplicate rows {what}{groups} ({rate:.1%}; limit {limit:.1%})"
                               + (f" on key {', '.join(t.key)}." if t.key else " (identical rows)."),
                               rate, None, limit, ref, pipeline, {**info, "duplicate_rows": p.duplicate_rows,
                                                                  "duplicate_groups": p.duplicate_groups, "null_key_rows": p.null_key_rows}))
        else:
            out.append(Outcome("duplicates", scope, "pass", site=pipeline, actual=rate))

    # freshness
    if p.latest is not None:
        age = (ctx.now - p.latest).total_seconds() / 3600
        fb = band("freshness_hours")
        limit = t.freshness_hours or max(26.0, (fb.high * 1.5) if fb else 26.0)
        if age > limit:
            out.append(Outcome("freshness", scope, "fail", "critical" if age > limit * 2 else "warning",
                               f"{t.table}: newest row is {age:.1f} h old ({p.date_column} = {p.latest:%a %d %b %H:%M} UTC); "
                               f"expected within {limit:.0f} h.", age, None, limit, ref, pipeline, info))
        else:
            out.append(Outcome("freshness", scope, "pass", site=pipeline, actual=age, expected_high=limit))

    # value ranges: negatives where the column has never had them
    neg = []
    for col, v in p.negatives.items():
        if not v or not p.profiled_rows or col in t.allow_negative:
            continue
        b = band("negative_rate", col)
        if b is None or b.median == 0:
            neg.append({"field": col, "rows": v, "rate": v / n})
    if neg:
        out.append(Outcome("value_range", scope, "fail", "warning",
                           f"{t.table}: negative values where there usually are none: "
                           + "; ".join(f"{x['field']} in {x['rows']:,} rows" for x in neg[:5]) + ".",
                           max(x["rate"] for x in neg), None, 0, ref, pipeline, {**info, "fields": neg}))
    elif p.negatives and p.profiled_rows:
        out.append(Outcome("value_range", scope, "pass", site=pipeline))

    # distribution: a numeric column's average, or a text column's most common value, far from its normal
    shifts = []
    for col, v in p.means.items():
        b = band("mean", col)
        if b is not None and (v < b.low or v > b.high) and abs(v - b.median) >= 0.25 * max(abs(b.median), 1e-9):
            shifts.append({"field": col, "kind": "average", "value": v, "normal": b.median,
                           "text": f"{col} averages {v:,.4g} (normally about {b.median:,.4g})"})
    for col, share in p.top_share.items():
        b = band("top_share", col)
        if b is not None and (share < b.low or share > b.high) and abs(share - b.median) >= 0.10:
            shifts.append({"field": col, "kind": "mix", "value": share, "normal": b.median,
                           "text": f"{col}: its most common value is now {share:.0%} of rows (normally {b.median:.0%})"})
    if shifts:
        out.append(Outcome("distribution_shift", scope, "fail", "warning",
                           f"{t.table}: the data's make-up changed: " + "; ".join(s["text"] for s in shifts[:4]) + ".",
                           len(shifts), None, 0, ref, pipeline, {**info, "fields": shifts}))
    elif p.means or p.top_share:
        out.append(Outcome("distribution_shift", scope, "pass", site=pipeline))

    # missing partitions: a table that gets rows every day, with days missing in the last week
    if p.days_recent is not None and p.days_before is not None and p.days_before >= 19:
        missing = 7 - p.days_recent
        if missing > 0:
            out.append(Outcome("missing_partitions", scope, "fail", "critical" if missing >= 3 else "warning",
                               f"{t.table}: no rows for {missing} of the last 7 days; it had rows on {p.days_before} of the 21 days before.",
                               missing, None, 0, ref, pipeline, {**info, "days_recent": p.days_recent, "days_before": p.days_before}))
        else:
            out.append(Outcome("missing_partitions", scope, "pass", site=pipeline, actual=0,
                               details={"days_recent": p.days_recent, "days_before": p.days_before}))

    # approved ranges (person-set limits) and values that don't read as their type
    outside = [{"field": c, "rows": v, "rate": v / n} for c, v in p.out_of_range.items() if v and p.profiled_rows]
    if outside:
        worst = max(x["rate"] for x in outside)
        out.append(Outcome("out_of_range", scope, "fail", "critical" if worst >= 0.05 else "warning",
                           f"{t.table}: values outside the allowed range: " + "; ".join(f"{x['field']} in {x['rows']:,} rows ({x['rate']:.1%})"
                                                                                         for x in outside[:5]) + ".",
                           worst, None, 0, ref, pipeline, {**info, "fields": outside}))
    elif p.out_of_range and p.profiled_rows:
        out.append(Outcome("out_of_range", scope, "pass", site=pipeline))
    bad_types = [{"field": c, "rows": v, "rate": v / n} for c, v in p.invalid.items() if v and p.profiled_rows]
    if bad_types:
        worst = max(x["rate"] for x in bad_types)
        out.append(Outcome("invalid_type", scope, "fail", "critical" if worst >= 0.05 else "warning",
                           f"{t.table}: values that don't read as their type: " + "; ".join(f"{x['field']} in {x['rows']:,} rows ({x['rate']:.1%})"
                                                                                            for x in bad_types[:5]) + ".",
                           worst, None, 0, ref, pipeline, {**info, "fields": bad_types}))
    elif p.invalid and p.profiled_rows:
        out.append(Outcome("invalid_type", scope, "pass", site=pipeline))

    # business rules
    for r in t.rules:
        bad = p.rule_fails.get(r.name, 0)
        rate = bad / n if p.profiled_rows else 0.0
        if p.profiled_rows and rate > r.max_fail_rate:
            out.append(Outcome("business_rule", scope, "fail", r.severity,
                               f"{t.table}: rule '{r.name}' ({r.sql}) broken by {bad:,} rows {what} ({rate:.1%}).",
                               rate, None, r.max_fail_rate, ref, pipeline, {**info, "rule": r.name, "sql": r.sql, "rows": bad}))
        else:
            out.append(Outcome("business_rule", scope, "pass", site=pipeline, details={"rule": r.name}))

    # referential integrity
    for k, bad in p.orphans.items():
        if bad > 0:
            out.append(Outcome("referential_integrity", scope, "fail", "warning",
                               f"{t.table}: {bad:,} rows {what} have a {k.split('→')[0]} with no match in {k.split('→')[1]}.",
                               bad / n, None, 0, ref, pipeline, {**info, "reference": k, "rows": bad}))
        else:
            out.append(Outcome("referential_integrity", scope, "pass", site=pipeline, details={"reference": k}))

    # schema
    current = [{"name": c.name, "type": c.type} for c in cols]
    snap = ctx.session.scalars(select(SchemaSnapshot).where(SchemaSnapshot.project_id == ctx.project.id, SchemaSnapshot.output_id == oid)
                               .order_by(SchemaSnapshot.captured_at.desc(), SchemaSnapshot.id.desc()).limit(1)).first()
    fp = schema_fingerprint(current)
    if snap is None:
        ctx.session.add(SchemaSnapshot(project_id=ctx.project.id, output_id=oid, columns=current, fingerprint=fp, captured_at=ctx.now))
        out.append(Outcome("schema_drift", scope, "pass", site=pipeline, message="Schema recorded as the baseline."))
    elif snap.fingerprint != fp:
        d = diff_schema(snap.columns, current)
        parts = ([f"removed {', '.join(d['removed'])}"] if d["removed"] else []) + \
                ([f"retyped {', '.join(f'{x['column']} ({x['from']} → {x['to']})' for x in d['retyped'])}"] if d["retyped"] else []) + \
                ([f"added {', '.join(d['added'])}"] if d["added"] else [])
        out.append(Outcome("schema_drift", scope, "fail", "critical" if d["removed"] or d["retyped"] else "warning",
                           f"{t.table} changed: {'; '.join(parts)}.", details={**d, "table": t.table, "output": oid, "current": current,
                                                                               "baseline_captured_at": iso(snap.captured_at)}, site=pipeline))
    else:
        out.append(Outcome("schema_drift", scope, "pass", site=pipeline))

    # remember this profile (after judging it, so today's values don't move their own baseline)
    ctx.session.add_all(MetricObservation(project_id=ctx.project.id, output_id=oid, site=pipeline, metric=m, field=f[:128],
                                          run_ref=ref, weekday=weekday, value=float(v), observed_at=ctx.now) for m, f, v in values)
    ctx.session.flush()
    return out


def quality_fingerprint(t: QualityTable) -> str:  # pragma: no cover - debugging aid
    return hashlib.sha1(json.dumps(t.model_dump(), sort_keys=True).encode()).hexdigest()[:10]
