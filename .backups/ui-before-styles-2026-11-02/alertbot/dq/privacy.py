"""The privacy gate. Three rules, enforced in code and covered by tests:

1. A data-quality query may only return ONE row of NAMED NUMBERS (counts, rates, approved statistics, a timestamp for
   freshness). Anything else (a second row, an unknown column, a string, bytes) is refused before it is kept anywhere:
   the check fails closed and nothing from that result is stored, logged, alerted or sent to an AI model.
2. Database errors never travel as raw text (they can quote values: "converting the varchar value 'John' to int").
   They become a fixed sentence chosen by error code.
3. Everything sent to an AI model passes `scrub_for_llm`, which removes values that messages from Data Factory or a
   database tend to quote. The AI payload for data quality is a strict model of numbers and identifiers only.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Kind = Literal["count", "number", "timestamp"]


class RawDataRejected(RuntimeError):
    """A query returned something that isn't an approved aggregate. Nothing from it was kept."""


@dataclass(frozen=True)
class Spec:
    """What one query is allowed to return: each column alias and the kind of number it must be."""

    columns: tuple[tuple[str, Kind], ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(a for a, _ in self.columns)


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d{1,9})?)?)?([+-]\d{2}:?\d{2}|Z)?$")


def _number(v: Any) -> float | int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        raise RawDataRejected("a true/false value, not a number")
    if isinstance(v, int):
        return v
    if isinstance(v, (float, Decimal)):
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return int(f) if isinstance(v, Decimal) and v == v.to_integral_value() else f
    raise RawDataRejected(f"a {type(v).__name__} value, not a number")


def _value(v: Any, kind: Kind) -> Any:
    if kind == "timestamp":
        if v is None or isinstance(v, (datetime, date)):
            return v
        if isinstance(v, str) and _ISO.match(v.strip()):  # SQLite returns dates as text: only ISO dates pass
            return datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        raise RawDataRejected(f"a {type(v).__name__} where a timestamp was expected")
    n = _number(v)
    if kind == "count" and n is not None and (n < 0 or n != int(n)):
        raise RawDataRejected("a count that isn't a whole number ≥ 0")
    return n


def validate_aggregate(rows: list[Any], keys: list[str] | tuple[str, ...], spec: Spec) -> dict[str, Any]:
    """The gate. `rows` straight from the driver, `keys` its column names. Returns {alias: value} or raises."""
    if len(rows) != 1:
        raise RawDataRejected(f"{len(rows)} rows returned; an aggregate is exactly one")
    got = tuple(str(k).lower() for k in keys)
    want = tuple(a.lower() for a in spec.names)
    if got != want:
        raise RawDataRejected("the result's columns aren't the approved ones")
    row = tuple(rows[0])
    if len(row) != len(want):
        raise RawDataRejected("the result has the wrong number of values")
    return {alias: _value(v, kind) for (alias, kind), v in zip(spec.columns, row, strict=True)}


# ── metadata (names and types of tables and columns: not data, but still checked) ──

_NAME = re.compile(r"^[^\x00-\x1f\x7f\[\]\"';]{1,128}$")
_TYPE = re.compile(r"^[A-Za-z0-9_ (),]{1,64}$")


def safe_name(v: Any) -> str:
    if not isinstance(v, str) or not _NAME.match(v) or v != v.strip():
        raise RawDataRejected("a table or column name the bot can't use safely")
    return v


def safe_type(v: Any) -> str:
    if not isinstance(v, str) or not _TYPE.match(v):
        raise RawDataRejected("a column type the bot doesn't recognise")
    return v


# ── database errors → fixed sentences ──

_CODES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\b18456\b|Login failed|Incorrect username or password|390100|390144", re.I), "login_failed",
     "The database refused the sign-in: check the user name and password."),
    (re.compile(r"\b4060\b|Cannot open database|does not exist or not authorized", re.I), "no_database",
     "The database doesn't exist, or this login can't open it."),
    (re.compile(r"\b(229|230|262|297|300)\b|permission was denied|Insufficient privileges", re.I), "no_permission",
     "This login isn't allowed to read that object."),
    (re.compile(r"\b208\b|Invalid object name|does not exist", re.I), "not_found", "That table or view wasn't found."),
    (re.compile(r"\b207\b|Invalid column name|invalid identifier", re.I), "bad_column", "A column the check expects isn't in the table."),
    (re.compile(r"HYT00|HYT01|timeout expired|Query timeout|statement.*timed out|000630", re.I), "timeout",
     "The query took longer than the time limit and was stopped."),
    (re.compile(r"\b1222\b|Lock request time out", re.I), "locked", "The table was locked by a load; the check will try again."),
    (re.compile(r"08001|08S01|\b53\b|\b10060\b|\b10061\b|TCP Provider|network-related|Could not connect|Name or service not known|"
                r"getaddrinfo|250001|Connection refused", re.I), "unreachable",
     "The database server can't be reached from the bot (address, port or firewall)."),
    (re.compile(r"certificate|SSL Provider|TLS", re.I), "certificate", "The server's encryption certificate wasn't trusted."),
    (re.compile(r"\b1205\b|deadlock", re.I), "deadlock", "The query was chosen as a deadlock victim; it will try again."),
    (re.compile(r"\b40613\b|\b40197\b|\b40501\b|\b49918\b|\b4221\b", re.I), "busy", "The database is busy or restarting; it will try again."),
    (re.compile(r"MFA|multi-factor|DUO|authenticator", re.I), "mfa", "The database asks for multi-factor sign-in, which a background job can't do."),
]

TRANSIENT = {"timeout", "locked", "deadlock", "busy", "unreachable"}


@dataclass(frozen=True)
class SafeError:
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def sanitize_error(exc: BaseException) -> SafeError:
    """A database error as a sentence we wrote ourselves. The driver's own text (which can quote values) is dropped."""
    if isinstance(exc, RawDataRejected):
        return SafeError("rejected", f"The result wasn't an approved aggregate, so it was discarded ({exc}).")
    text = f"{type(exc).__name__} {exc}"
    for pattern, code, message in _CODES:
        if pattern.search(text):
            return SafeError(code, message)
    return SafeError("error", "The database returned an error (details withheld: they can contain data).")


# ── the AI boundary ──

_SCRUBS: list[tuple[re.Pattern[str], str]] = [
    # "... the varchar value 'John Smith' ...", "value \"x\"", "values ('a', 'b')"
    (re.compile(r"(\bvalues?\b[^'\"()\n]{0,30}?)('(?:[^']|'')*'|\"[^\"]*\")", re.I), r"\1'…'"),
    # "duplicate key value is (48213)", "The conflicting value (…)", "key (12, 'x')"
    (re.compile(r"(\b(?:key|value|values)\b[^()\n]{0,30}?\bis\s*)\([^)]*\)", re.I), r"\1(…)"),
    (re.compile(r"(\b(?:duplicate key|conflicting value|key value)\b[^()\n]{0,20}?)\([^)]*\)", re.I), r"\1(…)"),
    # row-ish dumps: "Row: {...}", "firstRow": {...}
    (re.compile(r"(\"?(?:firstRow|row|record|item)s?\"?\s*[:=]\s*)(\{[^{}]*\}|\[[^\[\]]*\])", re.I), r"\1{…}"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
]


def scrub_for_llm(text: str) -> str:
    """Remove values a message may quote, before any text reaches an AI model."""
    if not text:
        return text
    for pattern, repl in _SCRUBS:
        text = pattern.sub(repl, text)
    return text


def scrub_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**m, "content": scrub_for_llm(m["content"])} if isinstance(m.get("content"), str) else m for m in messages]


# ── the data-quality report the AI may read: numbers and identifiers, nothing else ──

_IDENT = re.compile(r"^[^\x00-\x1f\x7f'\"\[\];{}<>]{0,200}$")  # names: no quotes, brackets, braces or control characters


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _ident(v: str) -> str:
    if not _IDENT.match(v):
        raise ValueError("not an identifier")
    return v


def _numbers(v: dict[str, Any]) -> dict[str, Any]:
    for k, x in v.items():
        _ident(k)
        if x is not None and (isinstance(x, bool) or not isinstance(x, (int, float))):
            raise ValueError("metrics are numbers only")
    return v


class AiCheck(_Strict):
    check: str
    status: Literal["pass", "fail", "skip"]
    severity: Literal["info", "warning", "critical"] | None = None
    field: str = ""
    # the bot's own sentence about the result (built from counts and names, scrubbed again here) and its numbers
    message: str = ""
    actual: float | None = None
    expected_low: float | None = None
    expected_high: float | None = None

    _v = field_validator("check", "field")(_ident)

    @field_validator("message")
    @classmethod
    def _message(cls, v: str) -> str:
        if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", v):
            raise ValueError("control characters")
        return scrub_for_llm(v)[:500]


class AiTable(_Strict):
    database: str
    schema_name: str = Field(alias="schema")
    table: str
    pipeline: str
    quality_status: Literal["OK", "WARNING", "CRITICAL", "LEARNING", "UNAVAILABLE"]
    quality_metrics: dict[str, float | int | None]
    checks: list[AiCheck] = Field(default_factory=list)
    object: str = ""
    pipeline_stats: dict[str, float | int | None] = Field(default_factory=dict)  # runs, failures, retries, durations
    empty_pct_by_column: dict[str, float | int | None] = Field(default_factory=dict)

    _v = field_validator("database", "schema_name", "table", "pipeline", "object")(_ident)
    _n = field_validator("quality_metrics", "pipeline_stats", "empty_pct_by_column")(_numbers)


class AiPayload(_Strict):
    """Exactly what the data-quality analysis may send to a model. Built with model_validate: anything else fails."""

    run_id: str
    project: str
    tables: list[AiTable]

    _v = field_validator("run_id", "project")(_ident)
