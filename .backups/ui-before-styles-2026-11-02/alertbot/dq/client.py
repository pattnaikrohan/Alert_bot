"""A connected database, used read-only and gently.

    · every statement is a template from dq.sql and passes the statement guard first;
    · it runs inside a transaction that is always rolled back (SQL Server: READ UNCOMMITTED, a lock timeout and low
      deadlock priority, so a check never blocks a load), with a per-query time limit;
    · at most `dq_max_concurrent` queries run at once across the bot, two per connection;
    · at most two rows are ever fetched, and the result must pass the privacy gate (one row of named numbers);
    · transient failures (timeouts, locks, deadlocks, a busy server) are retried twice;
    · logs carry the template name, the connection id and the time taken: never SQL text, parameters, values or errors'
      own wording.

The password / key is decrypted only to open the connection and lives in the connection factory, not in the URL.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

from ..settings import get_settings
from . import sql as T
from .privacy import (
    TRANSIENT,
    RawDataRejected,
    SafeError,
    safe_name,
    safe_type,
    sanitize_error,
    validate_aggregate,
)

log = logging.getLogger(__name__)
SQL_SERVER_SCOPE = "https://database.windows.net/.default"
_SQL_COPT_SS_ACCESS_TOKEN = 1256


class DbError(RuntimeError):
    """A sanitized database failure: `.safe` holds a sentence we wrote ourselves."""

    def __init__(self, safe: SafeError):
        super().__init__(safe.message)
        self.safe = safe


@dataclass
class ConnInfo:
    """A connection with its secret decrypted. Lives only in memory, never logged or returned."""

    id: int
    kind: str
    host: str
    port: int | None
    database: str
    auth: str
    username: str = ""
    secret: str = field(default="", repr=False)
    options: dict[str, Any] = field(default_factory=dict)
    home_account_id: str | None = None
    version: str = ""

    def __repr__(self) -> str:  # never print the secret, even by accident
        return f"ConnInfo(id={self.id}, kind={self.kind}, host={self.host}, database={self.database}, auth={self.auth})"


# ── concurrency ──

_LOCK = threading.Lock()
_GLOBAL: threading.BoundedSemaphore | None = None
_PER: dict[int, threading.BoundedSemaphore] = {}
_ENGINES: dict[int, tuple[str, sa.Engine]] = {}


def _global() -> threading.BoundedSemaphore:
    global _GLOBAL
    with _LOCK:
        if _GLOBAL is None:
            _GLOBAL = threading.BoundedSemaphore(get_settings().dq_max_concurrent)
        return _GLOBAL


def _per(conn_id: int) -> threading.BoundedSemaphore:
    with _LOCK:
        return _PER.setdefault(conn_id, threading.BoundedSemaphore(2))


def forget(conn_id: int) -> None:
    """Drop the pooled connections of a database (it was edited or removed)."""
    with _LOCK:
        old = _ENGINES.pop(conn_id, None)
    if old:
        old[1].dispose()


# ── engines ──


def _odbc_value(v: str) -> str:
    return "{" + v.replace("}", "}}") + "}"


def _sqlserver_driver() -> str:
    import pyodbc

    names = pyodbc.drivers()
    for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if d in names:
            return d
    raise DbError(SafeError("driver", "The bot's machine has no SQL Server ODBC driver (install ODBC Driver 18)."))


def sqlserver_connection_string(info: ConnInfo) -> str:
    """Encrypted (certificate verified unless the person turned that off), read-only intent, a login time limit."""
    opts = info.options
    # a named instance (host\instance) without a port is found through SQL Browser; otherwise TCP on the port (1433 by default)
    server = info.host if "\\" in info.host and not info.port else f"tcp:{info.host},{info.port or 1433}"
    parts = [f"DRIVER={{{_sqlserver_driver()}}}", f"SERVER={_odbc_value(server)}", f"DATABASE={_odbc_value(info.database)}",
             f"Encrypt={'yes' if opts.get('encrypt', True) else 'no'}",
             f"TrustServerCertificate={'yes' if opts.get('trust_server_certificate', False) else 'no'}",
             "Connection Timeout=15", "ApplicationIntent=ReadOnly", "APP=Alert Bot data checks"]
    if info.auth == "sql":
        parts += [f"UID={_odbc_value(info.username)}", f"PWD={_odbc_value(info.secret)}"]
    return ";".join(parts)


def open_sqlserver(info: ConnInfo, timeout: int) -> Any:
    """One pyodbc connection, set up so a check can never block a load or run forever."""
    import pyodbc

    conn_str = sqlserver_connection_string(info)
    if info.auth == "entra":
        from ..msauth import user_token

        token = user_token(info.home_account_id or "", SQL_SERVER_SCOPE)[0].encode("utf-16-le")
        conn = pyodbc.connect(conn_str, attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: struct.pack(f"<I{len(token)}s", len(token), token)})
    else:
        conn = pyodbc.connect(conn_str)
    conn.timeout = timeout  # per-query limit (seconds)
    cur = conn.cursor()
    cur.execute("SET LOCK_TIMEOUT 10000")  # never wait more than 10 s behind a load's locks
    cur.execute("SET DEADLOCK_PRIORITY LOW")  # if it comes to that, the check loses, not the load
    cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")  # read without taking shared locks
    cur.close()
    return conn


def _sqlserver_engine(info: ConnInfo, timeout: int) -> sa.Engine:
    return sa.create_engine("mssql+pyodbc://", creator=lambda: open_sqlserver(info, timeout), pool_size=2, max_overflow=0,
                            pool_pre_ping=True, pool_recycle=1800)


def _snowflake_engine(info: ConnInfo, timeout: int) -> sa.Engine:
    import snowflake.connector

    opts = info.options
    kwargs: dict[str, Any] = {"account": info.host, "user": info.username, "database": info.database,
                              "warehouse": opts.get("warehouse") or None, "role": opts.get("role") or None,
                              "login_timeout": 15, "network_timeout": timeout + 15, "client_session_keep_alive": False,
                              "session_parameters": {"STATEMENT_TIMEOUT_IN_SECONDS": timeout, "QUERY_TAG": "alertbot-data-checks"}}
    if info.auth == "keypair":
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_pem_private_key(info.secret.encode(), password=None)
        kwargs["private_key"] = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption())
    else:
        kwargs["password"] = info.secret
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    return sa.create_engine("snowflake://", creator=lambda: snowflake.connector.connect(**kwargs), pool_size=2, max_overflow=0,
                            pool_pre_ping=True, pool_recycle=1800)


def _sqlite_engine(info: ConnInfo) -> sa.Engine:  # tests only (the API refuses it outside dev mode)
    return sa.create_engine(f"sqlite:///{info.host}", connect_args={"check_same_thread": False})


def engine_for(info: ConnInfo) -> sa.Engine:
    with _LOCK:
        cached = _ENGINES.get(info.id)
        if cached and cached[0] == info.version:
            return cached[1]
    timeout = get_settings().dq_query_timeout_s
    if info.kind == "sqlserver":
        engine = _sqlserver_engine(info, timeout)
    elif info.kind == "snowflake":
        engine = _snowflake_engine(info, timeout)
    elif info.kind == "sqlite":
        engine = _sqlite_engine(info)
    else:
        raise DbError(SafeError("kind", f"Unsupported database type: {info.kind}"))
    with _LOCK:
        old = _ENGINES.get(info.id)
        _ENGINES[info.id] = (info.version, engine)
    if old and old[1] is not engine:
        old[1].dispose()
    return engine


# ── the client ──


# Exactly these statements may run though they aren't a SELECT: fixed text, no parameters, read-only by nature.
READ_ONLY_STATEMENTS = frozenset({"SHOW WAREHOUSES"})


class DbClient:
    def __init__(self, info: ConnInfo):
        self.info = info
        self.dialect = T.DIALECTS[info.kind]  # type: ignore[index]

    def _run(self, name: str, sql: str, params: dict[str, Any], max_rows: int) -> tuple[list[str], list[Any]]:
        if sql not in READ_ONLY_STATEMENTS:  # the few fixed, read-only statements that aren't a SELECT
            T.assert_read_only(sql)
        last: DbError | None = None
        for attempt in range(3):
            started = time.perf_counter()
            try:
                with _global(), _per(self.info.id):
                    engine = engine_for(self.info)
                    with engine.connect() as c:
                        try:
                            res = c.execute(sa.text(sql), params)
                            keys = list(res.keys())
                            rows = res.fetchmany(max_rows + 1)
                        finally:
                            c.rollback()  # whatever happened, nothing is ever committed
                log.info("dq %s on connection %s: %.2fs", name, self.info.id, time.perf_counter() - started)
                return keys, rows
            except DbError:
                raise
            except Exception as exc:  # noqa: BLE001 - every driver error is reduced to a fixed sentence
                safe = sanitize_error(exc)
                log.warning("dq %s on connection %s failed: %s (%s)", name, self.info.id, safe.code, type(exc).__name__)
                last = DbError(safe)
                if safe.code not in TRANSIENT or safe.code == "unreachable" and attempt:
                    break
                time.sleep((1, 3)[min(attempt, 1)])
        assert last is not None
        raise last

    def aggregate(self, q: T.Query) -> dict[str, Any]:
        """Run one approved aggregate template. Returns {alias: number}; raises RawDataRejected / DbError."""
        keys, rows = self._run(q.name, q.sql, q.params, max_rows=1)
        try:
            return validate_aggregate(rows, keys, q.spec)
        except RawDataRejected:
            log.error("dq %s on connection %s returned something that isn't an aggregate: discarded", q.name, self.info.id)
            raise

    def suspicious_values(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """The one exception to "numbers only": the distinct values breaking one value rule, for the person who connected
        this database to look at. At most 50 (value, count) pairs from one column, through their own gate; the caller
        returns them to that person and keeps nothing. Only the query's name is logged, never a value."""
        from .suspicious import MAX_VALUES, validate_values

        keys, rows = self._run("suspicious_values", sql, params, max_rows=MAX_VALUES)
        try:
            return validate_values(rows, keys)
        except RawDataRejected:
            log.error("dq suspicious_values on connection %s returned something unexpected: discarded", self.info.id)
            raise

    def _meta(self, mq: T.MetaQuery, limit: int = 5000) -> list[Any]:
        _, rows = self._run(mq.name, mq.sql, mq.params, max_rows=limit)
        return rows[:limit]

    def warehouses(self) -> tuple[list[str], list[Any]]:
        """Snowflake's SHOW WAREHOUSES (names, sizes, auto-suspend, resource monitors: settings, never data)."""
        return self._run("show_warehouses", "SHOW WAREHOUSES", {}, max_rows=500)

    def meta(self, mq: T.MetaQuery, limit: int = 5000) -> list[Any]:
        """A catalogue query (names, sizes, times; never a table's rows), through the read-only guard."""
        return self._meta(mq, limit)

    def tables(self) -> list[dict[str, str]]:
        out = []
        for schema, table, kind in self._meta(T.list_tables(self.dialect)):
            try:
                out.append({"schema": safe_name(str(schema)), "table": safe_name(str(table)),
                            "kind": "view" if str(kind).strip().upper() in ("V", "VIEW") else "table"})
            except RawDataRejected:
                continue  # a name we can't quote safely is simply not offered
        return out

    def columns(self, schema: str, table: str) -> list[tuple[str, str]]:
        out = []
        for name, type_name in self._meta(T.list_columns(self.dialect, schema, table)):
            try:
                out.append((safe_name(str(name)), safe_type(str(type_name or "unknown"))))
            except RawDataRejected:
                continue
        return out

    def test(self) -> dict[str, Any]:
        """Connect, prove we can read metadata, and find out whether this login could change data."""
        self._meta(T.list_tables(self.dialect), limit=1)
        probe = T.write_probe(self.dialect)
        can_write = None
        if probe is not None:
            try:
                can_write = bool(self.aggregate(probe)["m0"])
            except (DbError, RawDataRejected):
                can_write = None
        return {"ok": True, "can_write": can_write}
