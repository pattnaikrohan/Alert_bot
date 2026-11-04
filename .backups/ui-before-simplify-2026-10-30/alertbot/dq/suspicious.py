"""Suspicious values: rules about what a column's values should look like, worked out from the table's and columns'
NAMES only (a port name has no digits; a UN/LOCODE is two letters and three letters or digits; a latitude is between
-90 and 90), then checked against the data.

Who decides the rules: the rules bot (ai/value_rules.py), which only ever sees the table's name and its columns' names
and types, never a value; without AI, the same kind of rules from the names alone (`from_names`). Either way a rule is
one of a fixed set of KINDS with validated parameters: nobody's text ever becomes SQL. Each kind is written into SQL
here, per database.

What leaves the database:
    counts      how many rows break each rule (all rows, and today's): numbers only, through the privacy gate, kept
                and shown to everyone in the project, like every other check.
    values      the distinct values that break ONE rule, at most 50, each with its count, and only that column (never
                a whole row). Fetched only when the person who connected the database opens them; shown to them only;
                never stored, logged, alerted, or sent to an AI model. Sensitive columns, and anything that looks like
                personal data, are never checked or shown.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import BotState, DqMapping
from ..settings import get_settings
from ..timeutil import iso, utcnow
from . import sql as T
from .privacy import RawDataRejected, Spec

log = logging.getLogger(__name__)

RULES_KEY = "valuerules:{}"
COUNTS_KEY = "suspicious:{}"
MAX_RULES = 40
MAX_VALUES = 50
# never checked, never shown: personal data and secrets (and anything a person marked sensitive)
PERSONAL = re.compile(r"pass(word)?|secret|token|ssn|social|national.?id|passport|card|iban|account.?(no|num)|phone|mobile|e.?mail|"
                      r"\bdob\b|birth|salary|first.?name|last.?name|surname|full.?name|person|contact|address", re.I)

ColType = Literal["text", "number", "date"]
Kind = Literal["no_digits", "no_symbols", "trimmed", "length", "uppercase", "placeholder", "format", "range", "date_sane",
               "pattern", "forbid", "allowed_values", "lowercase"]
CharClass = Literal["digits", "letters", "symbols", "spaces"]
# a value's shape: {L} a letter, {D} a digit, {A} a letter or digit, each with an optional count ({L2}, {D7}); anything
# else is itself (letters, digits, space - / . :). E.g. a UN/LOCODE "{L2}{A3}", an IMO number "IMO{D7}", a date "{D4}-{D2}-{D2}".
SHAPE = re.compile(r"^(?:\{[LDA](?:[1-9]\d?)?\}|[A-Za-z0-9 \-/.:])+$")
SHAPE_TOKEN = re.compile(r"\{([LDA])([1-9]\d?)?\}|([A-Za-z0-9 \-/.:])")
SYMBOLS = "@#$%^*=+<>?!~|{}_"
PLACEHOLDERS = ["N/A", "NA", "#N/A", "NULL", "NONE", "NIL", "-", "--", "?", "TBD", "TBA", "TBC", "UNKNOWN", "TEST", "DUMMY", "XXX", "."]  # not "0" or "X": often real values

# format name → (what people call it, SQL Server LIKE patterns (any may match; whole value), Snowflake regex, SQLite GLOB patterns)
FORMATS: dict[str, tuple[str, list[str], str, list[str]]] = {
    "iso2": ("a 2-letter ISO country code (US, SG)", ["[A-Z][A-Z]"], "^[A-Za-z]{2}$", ["[A-Za-z][A-Za-z]"]),
    "iso3": ("a 3-letter ISO country code (USA, SGP)", ["[A-Z][A-Z][A-Z]"], "^[A-Za-z]{3}$", ["[A-Za-z][A-Za-z][A-Za-z]"]),
    "currency": ("a 3-letter currency code (USD, EUR)", ["[A-Z][A-Z][A-Z]"], "^[A-Za-z]{3}$", ["[A-Za-z][A-Za-z][A-Za-z]"]),
    "unlocode": ("a UN/LOCODE (SGSIN, NLRTM)", ["[A-Z][A-Z][A-Z2-9][A-Z2-9][A-Z2-9]", "[A-Z][A-Z] [A-Z2-9][A-Z2-9][A-Z2-9]"],
                 "^[A-Za-z]{2} ?[A-Za-z2-9]{3}$", ["[A-Za-z][A-Za-z][A-Za-z2-9][A-Za-z2-9][A-Za-z2-9]"]),
    "imo": ("an IMO ship number (7 digits)", ["[0-9][0-9][0-9][0-9][0-9][0-9][0-9]", "IMO[0-9][0-9][0-9][0-9][0-9][0-9][0-9]",
                                               "IMO [0-9][0-9][0-9][0-9][0-9][0-9][0-9]"], "^(IMO ?)?[0-9]{7}$", ["[0-9][0-9][0-9][0-9][0-9][0-9][0-9]"]),
    "mmsi": ("an MMSI (9 digits)", ["[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"], "^[0-9]{9}$", ["[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"]),
    "container": ("a container number (4 letters, 7 digits)", ["[A-Z][A-Z][A-Z][A-Z][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"], "^[A-Za-z]{4}[0-9]{7}$",
                  ["[A-Za-z][A-Za-z][A-Za-z][A-Za-z][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"]),
    "digits": ("digits only", [], "^[0-9]+$", []),  # SQL Server / SQLite: "has a non-digit" below
}
FITS: dict[str, tuple[ColType, ...]] = {"no_digits": ("text",), "no_symbols": ("text",), "trimmed": ("text",), "length": ("text",),
                                        "uppercase": ("text",), "placeholder": ("text",), "format": ("text",), "range": ("number",),
                                        "date_sane": ("date",), "pattern": ("text",), "forbid": ("text",), "allowed_values": ("text",),
                                        "lowercase": ("text",)}


def shape_tokens(shape: str) -> list[tuple[str, int | str]]:
    """"{L2}{A3}" → [("L", 2), ("A", 3)]; literal characters as ("=", ch)."""
    out: list[tuple[str, int | str]] = []
    for cls, n, lit in SHAPE_TOKEN.findall(shape):
        out.append((cls, int(n or 1)) if cls else ("=", lit))
    return out


def describe_shape(shape: str) -> str:
    """"{L2}{A3}" → "2 letters, then 3 letters or digits"; literal text in quotes."""
    words = {"L": ("letter", "letters"), "D": ("digit", "digits"), "A": ("letter or digit", "letters or digits")}
    parts: list[str] = []
    lit = ""
    for kind, n in shape_tokens(shape):
        if kind == "=":
            lit += str(n)
            continue
        if lit:
            parts.append(f'"{lit}"')
            lit = ""
        parts.append(f"{n} {words[kind][int(n) != 1]}")
    if lit:
        parts.append(f'"{lit}"')
    return ", then ".join(parts)


class ValueRule(BaseModel):
    """One rule about one column. Only these kinds and parameters exist; anything else is refused."""

    model_config = ConfigDict(extra="forbid")
    id: str = ""
    column: str = Field(min_length=1, max_length=128)
    kind: Kind
    min: float | None = None          # length / range
    max: float | None = None
    format: str | None = None         # format
    min_year: int | None = Field(default=None, ge=1800, le=2100)    # date_sane
    future_days: int | None = Field(default=None, ge=0, le=36500)   # date_sane
    shapes: list[str] | None = Field(default=None, max_length=6)     # pattern: any of these shapes
    chars: list[CharClass] | None = Field(default=None, max_length=4)  # forbid: none of these
    allowed: list[str] | None = Field(default=None, max_length=50)   # allowed_values (compared in capitals, trimmed)
    reason: str = Field(default="", max_length=240)
    confidence: Literal["high", "medium", "low"] = "medium"
    source: Literal["ai", "names", "person"] = "names"
    enabled: bool = True
    kept_on: bool = False  # a person switched it on: the "most rows break it" safety net leaves it alone

    @field_validator("reason")
    @classmethod
    def _plain(cls, v: str) -> str:
        return re.sub(r"[\x00-\x1f\x7f]", " ", v).strip()

    @field_validator("shapes")
    @classmethod
    def _shapes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = []
        for s in v:
            s = str(s).strip()
            if not s or len(s) > 40 or not SHAPE.match(s) or sum(int(n) if k != "=" else 1 for k, n in shape_tokens(s)) > 60:
                raise ValueError(f"{s!r} isn't a shape ({{L}} letter, {{D}} digit, {{A}} either, with counts; letters, digits and space - / . : as themselves)")
            out.append(s)
        return list(dict.fromkeys(out))

    @field_validator("allowed")
    @classmethod
    def _allowed(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = [re.sub(r"[\x00-\x1f\x7f]", "", str(x)).strip()[:60] for x in v]
        return list(dict.fromkeys(x for x in out if x))

    def checked(self, col_type: ColType) -> ValueRule:
        """This rule, if it makes sense for a column of this type; else ValueError (said plainly)."""
        if col_type not in FITS[self.kind]:
            raise ValueError(f"'{label(self)}' doesn't fit {self.column}, a {col_type} column")
        if self.kind == "format" and self.format not in FORMATS:
            raise ValueError(f"unknown format {self.format!r}")
        if self.kind in ("length", "range"):
            for v in (self.min, self.max):
                if v is not None and (math.isnan(v) or math.isinf(v)):
                    raise ValueError("limits must be numbers")
            if self.min is None and self.max is None:
                raise ValueError(f"'{self.kind}' needs a minimum or a maximum")
            if self.min is not None and self.max is not None and self.min > self.max:
                raise ValueError("the minimum is above the maximum")
            if self.kind == "length" and ((self.min or 0) < 0 or (self.max or 0) > 4000):
                raise ValueError("a length is between 0 and 4,000 characters")
        if self.kind == "date_sane" and self.min_year is None and self.future_days is None:
            raise ValueError("a date rule needs an earliest year or how far ahead dates may be")
        if self.kind == "pattern" and not self.shapes:
            raise ValueError("a shape rule needs at least one shape")
        if self.kind == "forbid" and not self.chars:
            raise ValueError("say which characters must never appear")
        if self.kind == "allowed_values" and not self.allowed:
            raise ValueError("list the values that are allowed")
        keep = {"min", "max"} if self.kind in ("length", "range") else {"format"} if self.kind == "format" else \
            {"min_year", "future_days"} if self.kind == "date_sane" else {"shapes"} if self.kind == "pattern" else \
            {"chars"} if self.kind == "forbid" else {"allowed"} if self.kind == "allowed_values" else set()
        clean = {k: getattr(self, k) for k in ("min", "max", "format", "min_year", "future_days", "shapes", "chars", "allowed") if k not in keep}
        out = self.model_copy(update=dict.fromkeys(clean))
        if out.chars:
            out = out.model_copy(update={"chars": sorted(set(out.chars))})
        sig = f"{out.column}|{out.kind}|{out.min}|{out.max}|{out.format}|{out.min_year}|{out.future_days}"
        if out.shapes or out.chars or out.allowed:  # (kept off the older kinds, so their ids, and people's choices, stay)
            sig += f"|{out.shapes}|{out.chars}|{sorted(x.upper() for x in out.allowed or [])}"
        return out.model_copy(update={"id": "r" + hashlib.sha1(sig.encode()).hexdigest()[:10]})


def _n(v: float | None) -> str:
    return "" if v is None else f"{v:g}"


def label(r: ValueRule) -> str:
    """The rule in words: "has no digits", "looks like a UN/LOCODE (SGSIN, NLRTM)"."""
    if r.kind == "no_digits":
        return "has no digits"
    if r.kind == "no_symbols":
        return "has no symbols like @ # $ % * _"
    if r.kind == "trimmed":
        return "has no spaces at the start or end"
    if r.kind == "uppercase":
        return "is in capitals"
    if r.kind == "lowercase":
        return "is in lower case"
    if r.kind == "pattern":
        return "looks like " + " or ".join(describe_shape(s) for s in r.shapes or [])
    if r.kind == "forbid":
        return "has no " + " or ".join(r.chars or [])
    if r.kind == "allowed_values":
        vals = r.allowed or []
        return "is one of " + ", ".join(vals[:8]) + (f" (+{len(vals) - 8} more)" if len(vals) > 8 else "")
    if r.kind == "placeholder":
        return "isn't a placeholder (N/A, NULL, TBD, -, TEST)"
    if r.kind == "format":
        return f"looks like {FORMATS.get(r.format or '', ('a known format',))[0]}"
    if r.kind == "length":
        return (f"is {_n(r.min)} to {_n(r.max)} characters long" if r.min is not None and r.max is not None
                else f"is at least {_n(r.min)} characters long" if r.min is not None else f"is at most {_n(r.max)} characters long")
    if r.kind == "range":
        return (f"is between {_n(r.min)} and {_n(r.max)}" if r.min is not None and r.max is not None
                else f"is at least {_n(r.min)}" if r.min is not None else f"is at most {_n(r.max)}")
    parts = []
    if r.min_year is not None:
        parts.append(f"not before {r.min_year}")
    if r.future_days is not None:
        parts.append("not in the future" if r.future_days <= 1 else f"not more than {r.future_days // 365 or 1} year{'s' if r.future_days >= 730 else ''} ahead"
                     if r.future_days >= 365 else f"not more than {r.future_days} days ahead")
    return "is a believable date (" + ", ".join(parts) + ")"


# ── a rule in SQL: true when a value BREAKS it (written here per database; the values are bound, never pasted) ──


def broken(d: T.Dialect, r: ValueRule, binds: dict[str, Any], i: int, now: datetime) -> str:
    c = d.q(r.column)
    db = d.name
    if r.kind == "no_digits":
        return {"sqlserver": f"{c} LIKE '%[0-9]%'", "snowflake": f"REGEXP_LIKE({c}, '.*[0-9].*', 's')"}.get(db, f"{c} GLOB '*[0-9]*'")
    if r.kind == "no_symbols":
        cls = "[" + SYMBOLS + "]"
        return {"sqlserver": "PATINDEX('%" + cls + "%', " + c + ") > 0", "snowflake": "REGEXP_LIKE(" + c + ", '.*" + cls + ".*', 's')"}.get(
            db, c + " GLOB '*" + cls + "*'")
    if r.kind == "trimmed":
        return f"DATALENGTH({c}) <> DATALENGTH(LTRIM(RTRIM({c})))" if db == "sqlserver" else f"{c} <> TRIM({c})"
    if r.kind == "uppercase":
        return f"CAST({c} AS VARBINARY(MAX)) <> CAST(UPPER({c}) AS VARBINARY(MAX))" if db == "sqlserver" else f"{c} <> UPPER({c})"
    if r.kind == "lowercase":
        return f"CAST({c} AS VARBINARY(MAX)) <> CAST(LOWER({c}) AS VARBINARY(MAX))" if db == "sqlserver" else f"{c} <> LOWER({c})"
    if r.kind == "forbid":
        return "(" + " OR ".join(_has(d, c, cls) for cls in r.chars or []) + ")"
    if r.kind == "allowed_values":
        names = [f"a{i}_{k}" for k in range(len(r.allowed or []))]
        binds.update(zip(names, [v.upper() for v in r.allowed or []], strict=True))
        trim = f"LTRIM(RTRIM({c}))" if db == "sqlserver" else f"TRIM({c})"
        return f"({trim} <> '' AND UPPER({trim}) NOT IN ({', '.join(':' + n for n in names)}))"
    if r.kind == "pattern":
        return "NOT (" + " OR ".join(_shape_sql(d, c, s) for s in r.shapes or []) + ")"
    if r.kind == "placeholder":
        names = [f"p{i}_{k}" for k in range(len(PLACEHOLDERS))]
        binds.update(zip(names, PLACEHOLDERS, strict=True))
        trim = f"LTRIM(RTRIM({c}))" if db == "sqlserver" else f"TRIM({c})"
        return f"UPPER({trim}) IN ({', '.join(':' + n for n in names)})"
    if r.kind == "format":
        _, likes, regex, globs = FORMATS[r.format or ""]
        if r.format == "digits":
            return {"sqlserver": f"{c} LIKE '%[^0-9]%'", "snowflake": f"NOT REGEXP_LIKE({c}, '{regex}')"}.get(db, f"{c} GLOB '*[^0-9]*'")
        if db == "sqlserver":
            return "NOT (" + " OR ".join(f"{c} LIKE '{p}'" for p in likes) + ")"
        if db == "snowflake":
            return f"NOT REGEXP_LIKE({c}, '{regex}', 'i')"
        return "NOT (" + " OR ".join(f"{c} GLOB '{p}'" for p in globs) + ")"
    if r.kind in ("length", "range"):
        x = (f"LEN({c})" if db == "sqlserver" else f"LENGTH({c})") if r.kind == "length" else c
        conds = []
        if r.min is not None:
            binds[f"lo{i}"] = r.min
            conds.append(f"{x} < :lo{i}")
        if r.max is not None:
            binds[f"hi{i}"] = r.max
            conds.append(f"{x} > :hi{i}")
        return "(" + " OR ".join(conds) + ")"
    conds = []
    if r.min_year is not None:
        binds[f"lo{i}"] = _when(d, datetime(r.min_year, 1, 1))
        conds.append(f"{c} < :lo{i}")
    if r.future_days is not None:
        binds[f"hi{i}"] = _when(d, now + timedelta(days=max(1, r.future_days)))
        conds.append(f"{c} > :hi{i}")
    return "(" + " OR ".join(conds) + ")"


def _has(d: T.Dialect, c: str, cls: str) -> str:
    """True when the value contains a character of this class."""
    db = d.name
    if cls == "spaces":
        return f"{c} LIKE '% %'"
    if cls == "symbols":
        k = "[" + SYMBOLS + "]"
        return {"sqlserver": "PATINDEX('%" + k + "%', " + c + ") > 0", "snowflake": "REGEXP_LIKE(" + c + ", '.*" + k + ".*', 's')"}.get(
            db, c + " GLOB '*" + k + "*'")
    k = "[0-9]" if cls == "digits" else "[A-Za-z]"
    return {"sqlserver": f"{c} LIKE '%{k}%'", "snowflake": f"REGEXP_LIKE({c}, '.*{k}.*', 's')"}.get(db, f"{c} GLOB '*{k}*'")


def _shape_sql(d: T.Dialect, c: str, shape: str) -> str:
    """True when the whole value has this shape. Only our own classes and the checked literal characters are written."""
    cls = {"sqlserver": {"L": "[A-Z]", "D": "[0-9]", "A": "[A-Z0-9]"}, "snowflake": {"L": "[A-Za-z]", "D": "[0-9]", "A": "[A-Za-z0-9]"},
           "sqlite": {"L": "[A-Za-z]", "D": "[0-9]", "A": "[A-Za-z0-9]"}}[d.name]
    toks = shape_tokens(shape)
    if d.name == "snowflake":
        rx = "".join(f"{cls[k]}{{{n}}}" if k != "=" else (str(n) if str(n).isalnum() else f"[{n}]") for k, n in toks)
        return f"REGEXP_LIKE({c}, '^{rx}$', 'i')"
    pat = "".join(cls[k] * int(n) if k != "=" else str(n) for k, n in toks)
    return f"{c} LIKE '{pat}'" if d.name == "sqlserver" else f"{c} GLOB '{pat}'"


def _when(d: T.Dialect, v: datetime) -> Any:
    return v.isoformat(sep=" ") if d.name == "sqlite" else v


def count_query(d: T.Dialect, schema: str, table: str, rules: list[ValueRule], date_column: str | None, start: datetime | None,
                now: datetime) -> T.Query:
    """One scan: rows, and for each rule how many rows break it (all, and today's when the table has a load date)."""
    binds: dict[str, Any] = {}
    exprs = [d.count]
    cols: list[tuple[str, Any]] = [("m0", "count")]
    today = f"{d.q(date_column)} >= :start" if date_column and start else None
    if today:
        exprs.append(f"SUM(CASE WHEN {today} THEN 1 ELSE 0 END)")
        cols.append(("m1", "count"))
    for i, r in enumerate(rules):
        cond = f"{d.q(r.column)} IS NOT NULL AND {broken(d, r, binds, i, now)}"
        exprs.append(f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)")
        cols.append((f"m{len(cols)}", "count"))
        if today:
            exprs.append(f"SUM(CASE WHEN {cond} AND {today} THEN 1 ELSE 0 END)")
            cols.append((f"m{len(cols)}", "count"))
    if today:
        binds["start"] = _when(d, start)  # type: ignore[arg-type]
    sql = f"SELECT {', '.join(f'{e} AS m{k}' for k, e in enumerate(exprs))} FROM {d.fq(schema, table)}{d.suffix}"
    return T.Query("suspicious_counts", sql, binds, Spec(tuple(cols)))


def values_query(d: T.Dialect, schema: str, table: str, r: ValueRule, date_column: str | None, start: datetime | None,
                 now: datetime) -> tuple[str, dict[str, Any]]:
    """The distinct values breaking one rule, most frequent first, at most MAX_VALUES, with their counts. Only the
    rule's column is read: never another column, never a whole row."""
    binds: dict[str, Any] = {}
    c = d.q(r.column)
    where = [f"{c} IS NOT NULL", broken(d, r, binds, 0, now)]
    if date_column and start:
        where.append(f"{d.q(date_column)} >= :start")
        binds["start"] = _when(d, start)
    top = f"TOP {MAX_VALUES} " if d.name == "sqlserver" else ""
    limit = "" if d.name == "sqlserver" else f" LIMIT {MAX_VALUES}"
    sql = (f"SELECT {top}{c} AS v, {d.count} AS n FROM {d.fq(schema, table)} WHERE {' AND '.join(where)} "
           f"GROUP BY {c} ORDER BY n DESC{limit}{d.suffix}")
    T.assert_read_only(sql)
    return sql, binds


def validate_values(rows: list[Any], keys: list[str]) -> list[dict[str, Any]]:
    """The gate for the one place values come back: exactly (v, n) pairs, at most MAX_VALUES, n a count, v shown as
    text (cut at 200 characters, control characters made visible). Anything else is refused."""
    if [str(k).lower() for k in keys] != ["v", "n"]:
        raise RawDataRejected("the result's columns aren't the approved ones")
    if len(rows) > MAX_VALUES:
        raise RawDataRejected("more values came back than asked for")
    out = []
    for row in rows:
        v, n = tuple(row)
        if isinstance(n, bool) or not isinstance(n, (int, Decimal)) or int(n) < 0:
            raise RawDataRejected("a count that isn't a whole number")
        if isinstance(v, (bytes, bytearray, memoryview)):
            raise RawDataRejected("binary data")
        text = v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat() if isinstance(v, date) else str(v)
        out.append({"value": re.sub(r"[\x00-\x1f\x7f]", "·", text)[:200], "count": int(n), "length": len(text)})
    return out


# ── rules from the names alone (what the rules bot does without AI, and a sanity floor) ───────────


def _tokens(name: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return {t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t}


PLACE = {"port", "country", "city", "region", "province", "state", "harbour", "harbor", "origin", "destination", "continent", "district",
         "town", "county", "pol", "pod", "polname", "podname"}
MAYBE_PLACE = {"terminal", "location", "place"}  # "Terminal 2", "Pier 1": sometimes digits belong (berths always can: not here)
CODE = {"code", "cd", "id", "no", "num", "locode", "unlocode", "iso", "iso2", "iso3", "key"}
FLAGS = {"is", "has", "flag", "bool", "yn", "active", "enabled", "deleted", "duplicate", "valid"}
AMOUNTS = {"count", "qty", "quantity", "amount", "price", "cost", "weight", "teu", "teus", "capacity", "duration", "length", "size",
           "volume", "total", "rows", "distance", "speed", "draft", "draught", "tonnage", "gt", "dwt"}
STAMP = {"created", "loaded", "load", "inserted", "updated", "modified", "ingested", "processed", "received", "etl", "batch", "published"}


ABBR = {"cd": "code", "no": "number", "num": "number", "nr": "number", "dt": "date", "ts": "timestamp", "pol": "port of loading",
        "pod": "port of discharge", "qty": "quantity", "teu": "TEU", "teus": "TEUs", "imo": "IMO", "mmsi": "MMSI", "eta": "estimated arrival",
        "etd": "estimated departure", "ata": "actual arrival", "atd": "actual departure", "ref": "reference", "desc": "description",
        "id": "id", "ccy": "currency", "cntr": "container", "ctr": "container", "wgt": "weight", "pct": "percentage", "lat": "latitude",
        "lon": "longitude", "lng": "longitude", "dob": "date of birth", "url": "URL", "uri": "URI", "iso": "ISO", "lang": "language"}


def meaning_from_name(col: str, typ: ColType) -> str:
    """A plain guess from the name alone: "POL_CD" → "port of loading code (text)"."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", col)
    words = [ABBR.get(t, t) for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]
    return (" ".join(words) or col) + {"text": "", "number": " (a number)", "date": " (a date)"}[typ]


def from_names(table: str, columns: list[tuple[str, ColType]]) -> list[ValueRule]:
    """Rules any reasonable person would agree with, from the column names alone."""
    out: list[ValueRule] = []

    def add(col: str, kind: str, why: str, conf: str = "high", **kw: Any) -> None:
        out.append(ValueRule(column=col, kind=kind, reason=why, confidence=conf, source="names", **kw))  # type: ignore[arg-type]

    for col, typ in columns:
        if PERSONAL.search(col):
            continue
        tk = _tokens(col)
        if typ == "text":
            if tk & {"unlocode", "locode"} or (tk & {"port", "location", "pol", "pod"} and tk & {"code", "cd", "locode"}):
                add(col, "format", "Port and location codes are UN/LOCODEs: 2 letters for the country, then 3 letters or digits.", format="unlocode")
                continue
            if "country" in tk and tk & {"code", "cd", "iso", "iso2", "iso3"}:
                fmt = "iso3" if tk & {"iso3", "alpha3", "3"} else "iso2"
                add(col, "format", f"Country codes are ISO codes ({'3' if fmt == 'iso3' else '2'} letters).", "medium", format=fmt)
                continue
            if tk & {"currency", "ccy", "curr"}:
                add(col, "format", "Currency codes are 3 letters (ISO 4217).", "medium", format="currency")
                continue
            if "imo" in tk:
                add(col, "format", "IMO ship numbers are 7 digits.", format="imo")
                continue
            if "mmsi" in tk:
                add(col, "format", "An MMSI is 9 digits.", format="mmsi")
                continue
            if tk & {"container", "cntr", "ctr"} and tk & {"no", "num", "number", "id"}:
                add(col, "format", "Container numbers are 4 letters and 7 digits (ISO 6346).", format="container")
                continue
            if tk & (PLACE | MAYBE_PLACE) and not tk & CODE:
                what = next(iter(sorted(tk & (PLACE | MAYBE_PLACE))))
                sure = bool(tk & PLACE)
                add(col, "no_digits", f"A {what} name is words: a digit in it means the value is wrong." if sure else
                    f"A {what} name is usually words; some (like Terminal 2) do have a digit, so check before trusting this one.",
                    "high" if sure else "medium")
                add(col, "no_symbols", f"A {what} name has no symbols like @ # $ % or underscores.")
                add(col, "placeholder", "Placeholders like N/A, NULL or TBD are missing values pretending to be real ones.")
                add(col, "trimmed", "Spaces at the start or end make the same name look like two different ones.", "medium")
                continue
            if "name" in tk:
                add(col, "placeholder", "Placeholders like N/A, NULL or TBD are missing values pretending to be real ones.", "medium")
                add(col, "trimmed", "Spaces at the start or end make the same name look like two different ones.", "medium")
                continue
            if tk & FLAGS:
                add(col, "allowed_values", "A yes/no flag is one of a few values.", "low",
                    allowed=["Y", "N", "YES", "NO", "TRUE", "FALSE", "T", "F", "1", "0"])
                continue
            if tk & {"code", "cd"} and not tk & FREE_TEXT:
                add(col, "forbid", "A code has no spaces inside it.", "medium", chars=["spaces"])
                add(col, "trimmed", "Spaces at the start or end make the same code look like two different ones.", "medium")
                continue
            if not tk & FREE_TEXT:
                add(col, "trimmed", "Spaces at the start or end make the same value look like two different ones.", "medium")
        elif typ == "number":
            if tk & {"lat", "latitude"}:
                add(col, "range", "A latitude is between -90 and 90.", min=-90, max=90)
            elif tk & {"lon", "lng", "long", "longitude"}:
                add(col, "range", "A longitude is between -180 and 180.", min=-180, max=180)
            elif tk & {"pct", "percent", "percentage"}:
                add(col, "range", "A percentage is between 0 and 100.", "medium", min=0, max=100)
            elif "imo" in tk:
                add(col, "range", "IMO ship numbers are 7 digits.", min=1000000, max=9999999)
            elif "mmsi" in tk:
                add(col, "range", "An MMSI is 9 digits.", min=100000000, max=999999999)
            elif tk & AMOUNTS:
                add(col, "range", f"A {next(iter(sorted(tk & AMOUNTS)))} can't be negative.", "medium", min=0)
        elif typ == "date":
            if tk & STAMP:
                add(col, "date_sane", "When a row was loaded or changed can't be in the future, or decades ago.", min_year=2000, future_days=1)
            else:
                add(col, "date_sane", "A date this far in the past, or more than ten years ahead, is almost certainly a typo.", "medium",
                    min_year=1950, future_days=3650)
    return out


# ── keeping rules and counts ──────────────────────────────────────────────────


def col_type(d: T.Dialect, type_name: str) -> ColType | None:
    k = d.kind_of(type_name)
    return k if k in ("text", "number", "date") else None  # type: ignore[return-value]


GUARD = 4  # bump when the guard changes: rules saved under an older one are re-checked (no AI call)
# free text, links and paths: symbols and digits are normal there
FREE_TEXT = {"url", "uri", "link", "href", "body", "text", "content", "description", "desc", "comment", "comments", "note", "notes",
             "summary", "title", "message", "msg", "json", "html", "xml", "payload", "path", "file", "filename", "query", "sql", "tags",
             "keywords", "remarks", "remark", "headline", "abstract", "address", "author", "authors", "source", "image", "img"}
# numbers, dates, ids and versions written as text: digits are their whole point
NUMBERISH = {"id", "no", "num", "number", "nr", "code", "date", "time", "datetime", "ts", "timestamp", "year", "month", "day", "hour",
             "version", "ver", "score", "sim", "wgt", "weight", "sentiment", "relevance", "rank", "amount", "qty", "quantity", "count",
             "price", "value", "rate", "pct", "percent", "voyage", "flight", "berth", "gate", "pier", "seq", "sequence", "hash", "key"}


def sane(r: ValueRule) -> str | None:
    """Why a rule from a bot (or the names) would flood people with false alarms; None if it's fine. People's own rules
    are theirs to judge."""
    if r.source == "person":
        return None
    tk = _tokens(r.column)
    if r.kind in ("no_symbols", "no_digits", "uppercase") and tk & FREE_TEXT:
        return f"{r.column} looks like free text, a link or a path: symbols and digits are normal there"
    if r.kind == "no_digits" and tk & NUMBERISH:
        return f"{r.column} looks like a number, date, code or id written as text: digits belong in it"
    classes = set(r.chars or []) | ({"digits"} if r.kind == "no_digits" else set()) | ({"symbols"} if r.kind == "no_symbols" else set())
    if r.kind == "forbid" and "digits" in classes and tk & (FREE_TEXT | NUMBERISH):
        return f"{r.column} looks like free text or a number written as text: digits belong in it"
    if r.kind == "forbid" and "symbols" in classes and tk & (FREE_TEXT | {"date", "time", "datetime", "ts", "timestamp"}):
        return f"{r.column} looks like free text, a link or a date: symbols belong in it"
    if r.kind == "forbid" and "letters" in classes and tk & (FREE_TEXT | {"name", "title", "label"}):
        return f"{r.column} is words: letters belong in it"
    if r.kind == "forbid" and "spaces" in classes and tk & (FREE_TEXT | {"name", "title", "label", "desc"}):
        return f"{r.column} is words: spaces belong in it"
    if r.kind in ("pattern", "allowed_values", "lowercase") and tk & (FREE_TEXT | {"name", "title"}):
        return f"{r.column} is free text or a name: it has no fixed shape or list of values"
    if r.kind == "placeholder" and tk & {"is", "has", "flag", "bool", "yn", "active", "enabled", "deleted", "duplicate", "valid", "status"}:
        return f"{r.column} looks like a flag or status: short values like 0, Y or - are real there"
    if r.kind == "no_symbols" and tk & {"date", "time", "datetime", "ts", "timestamp"}:
        return f"{r.column} looks like a date or time: - and : belong in it"
    return None


def clean(rules: list[ValueRule], columns: dict[str, ColType], sensitive: set[str],
          trusted: set[str] | frozenset[str] = frozenset()) -> tuple[list[ValueRule], list[str]]:
    """Keep only rules that fit a real, non-sensitive column and pass the guard; one of each (column, kind); at most
    MAX_RULES. A bot's low-confidence rules start switched off (a person can switch them on)."""
    out: dict[str, ValueRule] = {}
    dropped = []
    for r in rules:
        try:
            if r.column not in columns:
                raise ValueError(f"{r.column} isn't a column of this table")
            if r.column in sensitive or PERSONAL.search(r.column):
                raise ValueError(f"{r.column} is personal or sensitive: never checked")
            ok = r.checked(columns[r.column])
            if ok.column not in trusted and (why := sane(ok)):  # a person said what the column means: the name is no guide
                raise ValueError(why)
        except ValueError as exc:
            dropped.append(str(exc))
            continue
        if ok.source != "person" and ok.confidence == "low":
            ok = ok.model_copy(update={"enabled": False})
        out.setdefault(f"{ok.column}|{ok.kind}", ok)
    return list(out.values())[:MAX_RULES], dropped


def rules_of(session: Session, mapping_id: int) -> dict[str, Any]:
    row = session.get(BotState, RULES_KEY.format(mapping_id))
    return dict(row.value) if row and row.value else {}


def counts_of(session: Session, mapping_id: int) -> dict[str, Any]:
    row = session.get(BotState, COUNTS_KEY.format(mapping_id))
    return dict(row.value) if row and row.value else {}


def _put(session: Session, key: str, value: dict[str, Any]) -> None:
    row = session.get(BotState, key) or BotState(key=key, value={})
    row.value, row.updated_at = value, utcnow()
    session.add(row)
    session.flush()


def save_rules(session: Session, mapping_id: int, state: dict[str, Any]) -> None:
    _put(session, RULES_KEY.format(mapping_id), state)


def save_counts(session: Session, mapping_id: int, state: dict[str, Any]) -> None:
    _put(session, COUNTS_KEY.format(mapping_id), state)


def live_rules(state: dict[str, Any]) -> list[ValueRule]:
    return [r for r in (ValueRule.model_validate(x) for x in state.get("rules") or []) if r.enabled]


def merge_rules(prev: dict[str, Any], fresh: list[ValueRule], source: str, model: str | None, note: str | None, now: datetime) -> dict[str, Any]:
    """New rules from the bot, keeping what people decided: their own rules stay, and a rule they switched off stays off."""
    old = {x["id"]: ValueRule.model_validate(x) for x in prev.get("rules") or []}
    kept = [r for r in old.values() if r.source == "person"]
    off = {i for i, r in old.items() if not r.enabled}
    merged = {r.id: r for r in kept}
    removed = set(prev.get("removed") or [])
    for r in fresh:
        if r.id not in removed:
            merged.setdefault(r.id, r.model_copy(update={"enabled": r.enabled and r.id not in off}))
    return {**{k: v for k, v in prev.items() if k in ("columns_seen", "columns_at", "changes", "meanings", "removed")},
            "rules": [r.model_dump() for r in merged.values()], "generated_at": iso(now), "by": source, "model": model, "note": note,
            "guard": GUARD, "bot": BOT_VERSION}


BOT_VERSION = 3  # bump when the rules bot learns more: each table is asked again once (people's rules and choices stay)
COLUMNS_EVERY = timedelta(hours=1)  # how often each table's columns are looked at (names and types only)
TOO_COMMON = 0.5  # a bot's rule that most rows break is the rule's mistake, not the data's


def auto_off(state: dict[str, Any], counts: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Switch off any rule from a bot (or the names) that more than half the rows break, saying why. A person's own rule is
    left alone, and so is a rule a person switched back on after this."""
    rows = counts.get("rows") or 0
    if rows < 20:
        return state, []
    off = []
    rules = []
    for x in state.get("rules") or []:
        n = ((counts.get("counts") or {}).get(x["id"]) or {}).get("all") or 0
        if x.get("enabled") and x.get("source") != "person" and not x.get("kept_on") and n / rows > TOO_COMMON:
            x = {**x, "enabled": False, "reason": (f"Switched off by itself: {n / rows:.0%} of rows break it, so the rule is probably wrong, "
                                                  f"not the data. {x.get('reason') or ''}").strip()}
            off.append(x["id"])
        rules.append(x)
    return ({**state, "rules": rules} if off else state), off


def recheck(state: dict[str, Any]) -> dict[str, Any]:
    """Rules saved under an older guard, through today's (names only: no AI call, no database)."""
    keep, dropped = [], list(state.get("dropped") or [])
    for x in state.get("rules") or []:
        r = ValueRule.model_validate(x)
        if why := sane(r):
            dropped.append(why)
        else:
            keep.append(x)
    return {**state, "rules": keep, "dropped": dropped[-20:], "guard": GUARD}


# ── the work: ask for rules, count what breaks them ──────────────────────────


def table_shape(client: Any, m: DqMapping) -> tuple[dict[str, ColType], str | None]:
    """The table's columns (name → text / number / date) and its load-date column, from its metadata."""
    from .service import build_plan

    plan = build_plan(client, m)
    cols = {n: t for n, typ in client.columns(m.schema_name, m.table_name) if (t := col_type(client.dialect, typ))}
    return cols, plan.date_column


def generate(client: Any, m: DqMapping, use_ai: bool = True, only: list[str] | None = None, cols: dict[str, ColType] | None = None,
             meanings: dict[str, str] | None = None) -> tuple[list[ValueRule], str, str | None, str | None, list[str], dict[str, str]]:
    """(rules, who made them: ai | names, model, note, what was dropped, what each column means) for the whole table, or
    `only` some of its columns (the rest is context). `meanings` are people's own words, passed to the bot as facts.
    Network only: nothing is written here."""
    from ..ai import value_rules
    from .service import settings_of

    if cols is None:
        cols, _ = table_shape(client, m)
    sensitive = set(settings_of(m).sensitive)
    table = f"{m.connection.database}.{m.schema_name}.{m.table_name}"
    if use_ai:
        try:
            usable = [(c, t) for c, t in cols.items() if c not in sensitive and not PERSONAL.search(c)]
            proposed, model, found = value_rules.ask(table, usable, only, meanings)
            rules, dropped = clean([r for r in proposed if only is None or r.column in only], cols, sensitive, set(meanings or {}))
            found = {c: t for c, t in found.items() if only is None or c in only}
            return rules, "ai", model, None, dropped, found
        except value_rules.Unavailable as exc:
            note = f"The rules bot couldn't answer ({exc}); these rules come from the column names."
    else:
        note = None
    named = [r for r in from_names(m.table_name, list(cols.items())) if only is None or r.column in only]
    rules, dropped = clean(named, cols, sensitive)
    guessed = {c: meaning_from_name(c, t) for c, t in cols.items() if (only is None or c in only) and c not in sensitive and not PERSONAL.search(c)}
    return rules, "names", None, note, dropped, guessed


# ── meanings, and people's edits ───────────────────────────────────────────


def person_meanings(state: dict[str, Any]) -> dict[str, str]:
    return {c: m["text"] for c, m in (state.get("meanings") or {}).items() if m.get("by") == "person"}


def set_meanings(state: dict[str, Any], found: dict[str, str], by: str, now: datetime, who: str | None = None) -> dict[str, Any]:
    """The bot's meanings replace the bot's; a person's meaning is only replaced by a person."""
    cur = dict(state.get("meanings") or {})
    for c, text in found.items():
        if by == "person" or (cur.get(c) or {}).get("by") != "person":
            cur[c] = {"text": text, "by": by, "at": iso(now), **({"who": who} if who else {})}
    return {**state, "meanings": cur}


def replace_column(state: dict[str, Any], column: str, fresh: list[ValueRule]) -> dict[str, Any]:
    """The bot thought again about one column: its earlier rules there go; people's rules there, rules people removed
    (never re-added) and rules they switched off (stay off) are respected."""
    removed = set(state.get("removed") or [])
    old = [ValueRule.model_validate(x) for x in state.get("rules") or []]
    off = {r.id for r in old if not r.enabled}
    keep = [r for r in old if r.column != column or r.source == "person"]
    have = {r.id for r in keep}
    add = [r.model_copy(update={"enabled": r.enabled and r.id not in off}) for r in fresh if r.id not in have and r.id not in removed]
    return {**state, "rules": [r.model_dump() for r in keep + add]}


def extend_rules(prev: dict[str, Any], fresh: list[ValueRule], new: list[str], gone: list[str], now: datetime) -> dict[str, Any]:
    """The table changed: rules for its new columns join the others; rules on columns it no longer has go."""
    have = {x["id"] for x in prev.get("rules") or []} | set(prev.get("removed") or [])
    rules = [x for x in prev.get("rules") or [] if x["column"] not in gone] + [r.model_dump() for r in fresh if r.id not in have]
    note = []
    if new:
        note.append(f"New column{'s' if len(new) != 1 else ''} {', '.join(new[:6])}{'…' if len(new) > 6 else ''}: "
                    f"{len(fresh)} rule{'s' if len(fresh) != 1 else ''} added {ago_words(now)}")
    if gone:
        note.append(f"Column{'s' if len(gone) != 1 else ''} {', '.join(gone[:6])} left the table: {'their' if len(gone) != 1 else 'its'} rules went too")
    return {**prev, "rules": rules, "changes": [*(prev.get("changes") or []), {"at": iso(now), "new": new, "gone": gone,
                                                                              "added": len(fresh)}][-10:],
            "change_note": "; ".join(note) or None}


def ago_words(now: datetime) -> str:
    return f"on {now:%d %b %Y}"


def check(client: Any, m: DqMapping, rules: list[ValueRule], now: datetime) -> dict[str, Any]:
    """Count rows breaking each rule (numbers only, through the privacy gate). Network only: nothing is written."""
    from .service import today_start

    cols, date_column = table_shape(client, m)
    usable = [r for r in rules if r.column in cols]
    start = today_start(now, get_settings().tz) if date_column else None
    if not usable:
        return {"checked_at": iso(now), "rows": None, "rows_today": None, "counts": {}, "date_column": date_column, "columns": cols, "error": None}
    q = count_query(client.dialect, m.schema_name, m.table_name, usable, date_column, start, now)
    got = client.aggregate(q)
    vals = [got[f"m{k}"] or 0 for k in range(len(q.spec.columns))]
    step = 2 if date_column else 1
    base = 2 if date_column else 1
    counts = {r.id: {"all": int(vals[base + k * step]), "today": int(vals[base + k * step + 1]) if date_column else None}
              for k, r in enumerate(usable)}
    return {"checked_at": iso(now), "rows": int(vals[0]), "rows_today": int(vals[1]) if date_column else None, "counts": counts,
            "date_column": date_column, "columns": cols, "error": None}


def values(client: Any, m: DqMapping, rule: ValueRule, scope: Literal["all", "today"], now: datetime) -> list[dict[str, Any]]:
    """The values breaking one rule (see the module notes: shown to the database's owner only, never kept)."""
    from .service import settings_of, today_start

    if rule.column in set(settings_of(m).sensitive) or PERSONAL.search(rule.column):
        raise RawDataRejected("this column is sensitive: its values are never shown")
    cols, date_column = table_shape(client, m)
    if rule.column not in cols:
        raise RawDataRejected("that column isn't in the table any more")
    start = today_start(now, get_settings().tz) if scope == "today" and date_column else None
    sql, binds = values_query(client.dialect, m.schema_name, m.table_name, rule, date_column if start else None, start, now)
    return client.suspicious_values(sql, binds)


def scan_due(now: datetime | None = None) -> int:
    """Worker: give every checked table rules (once), and count what breaks them as often as its other checks run."""
    from ..db import session_scope
    from .service import checked, client_of, settings_of

    now = now or utcnow()
    with session_scope() as s:
        ids = [m.id for m in s.scalars(select(DqMapping)) if checked(m)]
    done = 0
    for mid in ids:
        with session_scope() as s:
            m = s.get(DqMapping, mid)
            if m is None:
                continue
            rs, cs = rules_of(s, mid), counts_of(s, mid)
            if rs.get("generated_at") and rs.get("guard") != GUARD:
                rs = recheck(rs)
                save_rules(s, mid, rs)
            every = timedelta(hours=settings_of(m).every_hours)
            due = not cs.get("checked_at") or now - _dt(cs["checked_at"]) >= every
            look = not rs.get("columns_at") or now - _dt(rs["columns_at"]) >= COLUMNS_EVERY
            stale = bool(rs.get("generated_at")) and rs.get("bot") != BOT_VERSION and rs.get("by") in ("ai", "names")
            if rs.get("generated_at") and not due and not look and not stale:
                continue
            conn = m.connection  # loaded while the session is open; both are used after it closes
            s.expunge(conn)
            s.expunge(m)
        try:
            client = client_of(conn)
            cols, _ = table_shape(client, m)
            if not rs.get("generated_at") or stale:
                rules, by, model, note, dropped, found = generate(client, m, cols=cols, meanings=person_meanings(rs))
                due = True
                rs = set_meanings(merge_rules(rs, rules, by, model, note, now) | {"dropped": dropped[:20]}, found, by, now)
            elif "columns_seen" in rs:
                seen = set(rs["columns_seen"])
                new = [c for c in cols if c not in seen]
                gone = sorted(seen - set(cols))
                if new or gone:
                    fresh, found = [], {}
                    if new:
                        fresh, by_new, _, _, _, found = generate(client, m, only=new, cols=cols, meanings=person_meanings(rs))
                    rs = extend_rules(rs, fresh, new, gone, now)
                    if found:
                        rs = set_meanings(rs, found, by_new, now)
                    if gone:
                        rs = {**rs, "meanings": {c: v for c, v in (rs.get("meanings") or {}).items() if c not in gone}}
                    due = True
            rs = {**rs, "columns_seen": sorted(cols), "columns_at": iso(now)}
            with session_scope() as s:
                save_rules(s, mid, rs)
            if not due:
                continue
            got = check(client, m, live_rules(rs), now)
            rs, off = auto_off(rs, got)
            if off:
                with session_scope() as s:
                    save_rules(s, mid, rs)
        except Exception as exc:  # noqa: BLE001 - shown on the page; the last good counts stay
            from .client import DbError
            msg = str(exc)[:300] if isinstance(exc, (DbError, RawDataRejected)) else "the table couldn't be checked just now"
            log.warning("suspicious-value check of mapping %s failed: %s", mid, type(exc).__name__)
            got = {**cs, "error": msg, "error_at": iso(now), "checked_at": cs.get("checked_at") or iso(now)}
        with session_scope() as s:
            save_counts(s, mid, got)
        done += 1
    return done


def _dt(s: str) -> datetime:
    v = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (v - v.utcoffset()).replace(tzinfo=None) if v.tzinfo else v
