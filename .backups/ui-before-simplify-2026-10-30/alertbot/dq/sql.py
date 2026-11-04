"""Approved query templates, per database dialect. Nothing else ever runs.

Every identifier comes from the database's own metadata for a table someone mapped (the allowlist) and passes
`privacy.safe_name` (no quotes, brackets, semicolons or control characters), then is quoted. Every value a person
typed (a rule's threshold, a range) is a bound parameter. Each template returns one row of aliased numbers
(m0, m1, …) described by a `Spec`, which the privacy gate enforces. `assert_read_only` is a last check on the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .privacy import Kind, Spec, safe_name

DialectName = Literal["sqlserver", "snowflake", "sqlite"]

# ── the statement guard ───────────────────────────────────────────────────────

_QUOTED = re.compile(r"\[[^\]]*\]|\"[^\"]*\"|'(?:[^']|'')*'")
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE|DENY|INTO|BACKUP|"
    r"RESTORE|SHUTDOWN|KILL|DBCC|OPENROWSET|OPENQUERY|OPENDATASOURCE|BULK|CALL|PUT|GET|COPY|REMOVE|UNDROP|USE|SET|"
    r"DECLARE|WAITFOR|RECONFIGURE|ATTACH|DETACH|PRAGMA|VACUUM|REPLACE)\b|\bxp_|\bsp_|--|/\*|;",
    re.I,
)


class UnsafeStatement(RuntimeError):
    pass


def assert_read_only(sql: str) -> None:
    """Refuse anything that isn't a single read-only SELECT (quoted names and literals are ignored by the scan)."""
    bare = _QUOTED.sub(" ", sql)
    if not re.match(r"^\s*(SELECT|WITH)\b", bare, re.I):
        raise UnsafeStatement("only SELECT statements run")
    m = _FORBIDDEN.search(bare)
    if m:
        raise UnsafeStatement(f"refused: '{m.group(0).strip()}' is not allowed in a data-quality query")


# ── dialects ──────────────────────────────────────────────────────────────────

ColKind = Literal["number", "text", "date", "other"]


@dataclass(frozen=True)
class Dialect:
    name: DialectName

    def q(self, ident: str) -> str:
        ident = safe_name(ident)
        return f"[{ident}]" if self.name == "sqlserver" else f'"{ident}"'

    def fq(self, schema: str, table: str) -> str:
        return f"{self.q(schema)}.{self.q(table)}"

    @property
    def count(self) -> str:
        return "COUNT_BIG(*)" if self.name == "sqlserver" else "COUNT(*)"

    @property
    def suffix(self) -> str:  # keep a big scan from taking over the server
        return " OPTION (MAXDOP 1)" if self.name == "sqlserver" else ""

    def as_float(self, col: str) -> str:
        return f"CAST({col} AS {'FLOAT' if self.name == 'sqlserver' else 'DOUBLE' if self.name == 'snowflake' else 'REAL'})"

    def stddev(self, col: str) -> str:
        if self.name == "sqlite":
            return "NULL"
        return f"{'STDEV' if self.name == 'sqlserver' else 'STDDEV'}({self.as_float(col)})"

    def kind_of(self, type_name: str) -> ColKind:
        t = type_name.lower().split("(")[0].strip()
        if t in {"int", "bigint", "smallint", "tinyint", "integer", "decimal", "numeric", "number", "float", "real", "double",
                 "double precision", "money", "smallmoney", "fixed", "byteint"}:
            return "number"
        if self.name == "sqlserver" and t in {"char", "varchar", "nchar", "nvarchar"}:
            return "text"
        if self.name != "sqlserver" and t in {"char", "varchar", "nchar", "nvarchar", "text", "string", "character"}:
            return "text"
        if t in {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "timestamp_ntz", "timestamp_ltz", "timestamp_tz"}:
            return "date"
        if t == "timestamp" and self.name != "sqlserver":  # in SQL Server "timestamp" is a row version, not a time
            return "date"
        return "other"  # text/ntext/xml/binary/variant…: only checked for emptiness (IS NULL)

    def invalid(self, col: str, as_type: str) -> str:
        """True when a text value can't be read as int / decimal / date (only counted, never returned)."""
        filled = f"{col} IS NOT NULL AND {col} <> ''"
        if self.name == "sqlserver":
            target = {"int": "BIGINT", "decimal": "DECIMAL(38, 10)", "date": "DATETIME2"}[as_type]
            return f"{filled} AND TRY_CONVERT({target}, {col}) IS NULL"
        if self.name == "snowflake":
            fn = {"int": f"TRY_TO_NUMBER({col})", "decimal": f"TRY_TO_DECIMAL({col}, 38, 10)", "date": f"TRY_TO_TIMESTAMP({col})"}[as_type]
            return f"{filled} AND {fn} IS NULL"
        check = {"int": f"TRIM({col}) GLOB '*[^0-9+-]*'", "decimal": f"TRIM({col}) GLOB '*[^0-9.+-]*'",
                 "date": f"date({col}) IS NULL"}[as_type]
        return f"{filled} AND {check}"


DIALECTS = {n: Dialect(n) for n in ("sqlserver", "snowflake", "sqlite")}


@dataclass(frozen=True)
class Query:
    name: str  # the template's name: the only thing logged about a query
    sql: str
    params: dict[str, Any]
    spec: Spec


@dataclass(frozen=True)
class MetaQuery:
    name: str
    sql: str
    params: dict[str, Any]


# ── metadata ─────────────────────────────────────────────────────────────────


def list_tables(d: Dialect) -> MetaQuery:
    if d.name == "sqlserver":
        sql = ("SELECT s.name AS schema_name, o.name AS table_name, o.type AS kind FROM sys.objects o "
               "JOIN sys.schemas s ON o.schema_id = s.schema_id WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0 "
               "ORDER BY s.name, o.name")
    elif d.name == "snowflake":
        sql = ("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
               "WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA' ORDER BY 1, 2")
    else:
        sql = "SELECT 'main', name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    return MetaQuery("list_tables", sql, {})


def list_columns(d: Dialect, schema: str, table: str) -> MetaQuery:
    if d.name == "sqlserver":
        sql = ("SELECT c.name AS column_name, ty.name AS type_name FROM sys.columns c "
               "JOIN sys.types ty ON c.user_type_id = ty.user_type_id WHERE c.object_id = OBJECT_ID(:obj) ORDER BY c.column_id")
        return MetaQuery("list_columns", sql, {"obj": d.fq(schema, table)})
    if d.name == "snowflake":
        sql = ("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = :s AND TABLE_NAME = :t "
               "ORDER BY ORDINAL_POSITION")
        return MetaQuery("list_columns", sql, {"s": safe_name(schema), "t": safe_name(table)})
    return MetaQuery("list_columns", "SELECT name, type FROM pragma_table_info(:t)", {"t": safe_name(table)})


def write_probe(d: Dialect) -> Query | None:
    """Can this login change data? (Shown to the person; the bot never writes either way.)"""
    if d.name != "sqlserver":
        return None
    sql = ("SELECT CAST(IS_SRVROLEMEMBER('sysadmin') AS INT) + CAST(IS_ROLEMEMBER('db_owner') AS INT) "
           "+ CAST(IS_ROLEMEMBER('db_datawriter') AS INT) + CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT') AS INT) "
           "+ CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE') AS INT) + CAST(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE') AS INT) AS m0")
    return Query("write_probe", sql, {}, Spec((("m0", "count"),)))


# ── the checks ────────────────────────────────────────────────────────────────


@dataclass
class Plan:
    """What to measure on one table, decided from its metadata and the person's settings (all names already allowlisted)."""

    schema: str
    table: str
    columns: dict[str, ColKind]
    date_column: str | None = None
    key: list[str] = field(default_factory=list)
    watched: list[str] = field(default_factory=list)       # null / empty counts
    numeric: list[str] = field(default_factory=list)       # negatives
    measures: list[str] = field(default_factory=list)      # averages (numbers that are amounts, not ids)
    stats: list[str] = field(default_factory=list)         # approved min / max / avg / stddev
    categories: list[str] = field(default_factory=list)    # share of the most common value (count only)
    ranges: list[tuple[str, float | None, float | None]] = field(default_factory=list)
    typed: list[tuple[str, str]] = field(default_factory=list)  # (text column, int|decimal|date)
    rules: list[tuple[str, str, str, list[Any]]] = field(default_factory=list)  # (name, column, op, values)
    references: list[tuple[str, str, str, str]] = field(default_factory=list)  # (column, ref schema, ref table, ref column)


RULE_OPS = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "=": "=", "<>": "<>", "between": "BETWEEN", "in": "IN", "not_in": "NOT IN"}


def _today(d: Dialect, p: Plan, alias: str = "") -> str:
    return f"{alias}{d.q(p.date_column)} >= :start" if p.date_column else ""


def rows_total(d: Dialect, p: Plan) -> Query:
    return Query("rows_total", f"SELECT {d.count} AS m0 FROM {d.fq(p.schema, p.table)}{d.suffix}", {}, Spec((("m0", "count"),)))


def latest(d: Dialect, p: Plan) -> Query | None:
    if not p.date_column:
        return None
    return Query("freshness", f"SELECT MAX({d.q(p.date_column)}) AS m0 FROM {d.fq(p.schema, p.table)}{d.suffix}", {},
                 Spec((("m0", "timestamp"),)))


@dataclass(frozen=True)
class Part:
    metric: str  # rows | null | empty | negative | mean | min | max | avg | stddev | out_of_range | invalid | rule | null_key
    field: str
    kind: Kind


def profile(d: Dialect, p: Plan, params: dict[str, Any]) -> tuple[Query, list[Part]]:
    """One scan of the newest rows (today's, by the date column; else the whole table) for every per-column count."""
    exprs: list[str] = [d.count]
    parts: list[Part] = [Part("rows", "", "count")]
    binds = dict(params)

    def add(expr: str, metric: str, fld: str, kind: Kind = "count") -> None:
        exprs.append(expr)
        parts.append(Part(metric, fld, kind))

    for c in p.watched:
        qc = d.q(c)
        if p.columns[c] == "text":
            add(f"SUM(CASE WHEN {qc} IS NULL OR {qc} = '' THEN 1 ELSE 0 END)", "null", c)
            add(f"SUM(CASE WHEN {qc} = '' THEN 1 ELSE 0 END)", "empty", c)
        else:
            add(f"SUM(CASE WHEN {qc} IS NULL THEN 1 ELSE 0 END)", "null", c)
    for c in p.numeric:
        add(f"SUM(CASE WHEN {d.q(c)} < 0 THEN 1 ELSE 0 END)", "negative", c)
    for c in p.measures:
        add(f"AVG({d.as_float(d.q(c))})", "mean", c, "number")
    for c in p.stats:
        qc = d.q(c)
        if p.columns[c] == "date":
            add(f"MIN({qc})", "min", c, "timestamp")
            add(f"MAX({qc})", "max", c, "timestamp")
        else:
            add(f"MIN({d.as_float(qc)})", "min", c, "number")
            add(f"MAX({d.as_float(qc)})", "max", c, "number")
            add(f"AVG({d.as_float(qc)})", "avg", c, "number")
            add(d.stddev(qc), "stddev", c, "number")
    for i, (c, lo, hi) in enumerate(p.ranges):
        conds = []
        if lo is not None:
            binds[f"lo{i}"] = lo
            conds.append(f"{d.q(c)} < :lo{i}")
        if hi is not None:
            binds[f"hi{i}"] = hi
            conds.append(f"{d.q(c)} > :hi{i}")
        if conds:
            add(f"SUM(CASE WHEN {' OR '.join(conds)} THEN 1 ELSE 0 END)", "out_of_range", c)
    for c, as_type in p.typed:
        add(f"SUM(CASE WHEN {d.invalid(d.q(c), as_type)} THEN 1 ELSE 0 END)", "invalid", c)
    for i, (name, c, op, values) in enumerate(p.rules):
        qc, sql_op = d.q(c), RULE_OPS[op]
        names = [f"r{i}_{k}" for k in range(len(values))]
        binds.update(dict(zip(names, values, strict=True)))
        if op == "between":
            cond = f"{qc} BETWEEN :{names[0]} AND :{names[1]}"
        elif op in ("in", "not_in"):
            cond = f"{qc} {sql_op} ({', '.join(':' + n for n in names)})"
        else:
            cond = f"{qc} {sql_op} :{names[0]}"
        add(f"SUM(CASE WHEN {qc} IS NOT NULL AND NOT ({cond}) THEN 1 ELSE 0 END)", "rule", name)
    if p.key:
        add(f"SUM(CASE WHEN {' OR '.join(f'{d.q(k)} IS NULL' for k in p.key)} THEN 1 ELSE 0 END)", "null_key", "")
    where = _today(d, p)
    sql = f"SELECT {', '.join(f'{e} AS m{i}' for i, e in enumerate(exprs))} FROM {d.fq(p.schema, p.table)}" \
          f"{' WHERE ' + where if where else ''}{d.suffix}"
    spec = Spec(tuple((f"m{i}", part.kind) for i, part in enumerate(parts)))
    return Query("profile", sql, binds if where else {k: v for k, v in binds.items() if k != "start"}, spec), parts


def duplicates(d: Dialect, p: Plan, params: dict[str, Any]) -> Query | None:
    """Rows beyond the first per business key (keys with an empty part don't count: they're reported as empty keys),
    and how many keys repeat. Without a key: identical rows (Snowflake only)."""
    where = _today(d, p)
    if p.key:
        keys = ", ".join(d.q(k) for k in p.key)
        conds = [f"{d.q(k)} IS NOT NULL" for k in p.key] + ([where] if where else [])
        sql = (f"SELECT COALESCE(SUM(n - 1), 0) AS m0, {d.count} AS m1 FROM (SELECT {d.count} AS n FROM {d.fq(p.schema, p.table)} "
               f"WHERE {' AND '.join(conds)} GROUP BY {keys} HAVING {d.count} > 1) g{d.suffix}")
    elif d.name == "snowflake":
        sql = f"SELECT COUNT(*) - COUNT(DISTINCT HASH(*)) AS m0, NULL AS m1 FROM {d.fq(p.schema, p.table)}{' WHERE ' + where if where else ''}"
    else:
        return None
    return Query("duplicates", sql, params if where else {}, Spec((("m0", "count"), ("m1", "count"))))


def top_share(d: Dialect, p: Plan, column: str, params: dict[str, Any]) -> Query:
    """How many rows the most common value has (its COUNT only: the value itself never leaves the database)."""
    where = _today(d, p)
    sql = (f"SELECT MAX(n) AS m0, {d.count} AS m1 FROM (SELECT {d.count} AS n FROM {d.fq(p.schema, p.table)}"
           f"{' WHERE ' + where if where else ''} GROUP BY {d.q(column)}) g{d.suffix}")
    return Query("distribution", sql, params if where else {}, Spec((("m0", "count"), ("m1", "count"))))


def orphans(d: Dialect, p: Plan, ref: tuple[str, str, str, str], params: dict[str, Any]) -> Query:
    col, rschema, rtable, rcol = ref
    conds = ([_today(d, p, "a.")] if p.date_column else []) + [f"a.{d.q(col)} IS NOT NULL",
             f"NOT EXISTS (SELECT 1 FROM {d.fq(rschema, rtable)} b WHERE b.{d.q(rcol)} = a.{d.q(col)})"]
    sql = f"SELECT {d.count} AS m0 FROM {d.fq(p.schema, p.table)} a WHERE {' AND '.join(conds)}{d.suffix}"
    return Query("referential_integrity", sql, params if p.date_column else {}, Spec((("m0", "count"),)))


def partitions(d: Dialect, p: Plan) -> Query | None:
    """Days that have rows: in the last 7 complete days, and in the 21 days before (counts of days only)."""
    if not p.date_column:
        return None
    col = d.q(p.date_column)
    day = f"date({col})" if d.name == "sqlite" else f"CAST({col} AS DATE)"
    sql = (f"SELECT COUNT(DISTINCT CASE WHEN {col} >= :w7 AND {col} < :t0 THEN {day} END) AS m0, "
           f"COUNT(DISTINCT CASE WHEN {col} >= :w28 AND {col} < :w7 THEN {day} END) AS m1 "
           f"FROM {d.fq(p.schema, p.table)} WHERE {col} >= :w28{d.suffix}")
    return Query("partitions", sql, {}, Spec((("m0", "count"), ("m1", "count"))))
