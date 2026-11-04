"""Feeds: tables written directly by scheduled producers (Azure Function Apps, Snowflake tasks / streams, scrapers).

`FeedSource` reads a feed's rows (optionally a URL-filtered slice of a shared table) through SQLAlchemy Core, so the
same code runs on Snowflake in production and on SQLite in tests. Every identifier comes from validated config and
every value is a bound parameter: there is no free-form SQL.

`FeedProducers` answers "did the producer itself fail?": Snowflake TASK_HISTORY for tasks and streams, and
Application Insights for Azure Function Apps (only when an App Insights app id is configured).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import sqlalchemy as sa
from sqlalchemy import Engine, create_engine, event

from ..config import FeedConfig, ProjectConfig, SourceConfig
from ..secrets import resolve_secret
from .base import ActivityRun, ColumnInfo, PipelineRun, WindowMetrics

log = logging.getLogger(__name__)


# ── engines ──────────────────────────────────────────────────────────────────


@lru_cache(maxsize=16)
def feed_engine(src: SourceConfig) -> Engine:
    if src.type == "sqlite":
        engine = create_engine(src.url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _attach(dbapi_conn, _rec):  # noqa: ANN001
            # Local stand-in for Snowflake schemas: <db>_<SCHEMA>.db files attached under the schema name.
            base = (src.url or "").split("///", 1)[-1]
            for schema in ("RAW_RISKMONITOR", "MRT_TERMINAL"):
                dbapi_conn.execute(f"ATTACH DATABASE '{base}.{schema.lower()}' AS {schema}")

        return engine
    if src.type == "snowflake":
        return _snowflake_engine(src)
    raise ValueError(f"feeds need a sqlite or snowflake source, not {src.type}")


def _snowflake_engine(src: SourceConfig) -> Engine:  # pragma: no cover - needs Snowflake
    try:
        from snowflake.sqlalchemy import URL  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("install the 'snowflake' extra: uv sync --extra snowflake") from exc
    account, user = resolve_secret(src.account), resolve_secret(src.user)
    if not account or not user:
        raise RuntimeError(f"Snowflake source '{src.id}': account/user are not set ({src.account}, {src.user})")
    url = URL(account=account, user=user, database=src.database, role=src.role or None, warehouse=src.warehouse or None)
    connect_args: dict[str, Any] = {"application": "AlertBot", "session_parameters": {"TIMEZONE": "UTC", "QUERY_TAG": "alertbot"}}
    if src.authenticator == "externalbrowser":
        # Interactive SSO (Microsoft sign-in + Authenticator). The id token is cached, so the bot prompts again only
        # when it expires (requires ALLOW_ID_TOKEN on the account). For the pilot only; production uses a service user.
        connect_args.update(authenticator="externalbrowser", client_store_temporary_credential=True)
    else:
        from cryptography.hazmat.primitives import serialization

        pem = resolve_secret(src.private_key)
        if not pem:
            raise RuntimeError(f"Snowflake source '{src.id}': snowflake_jwt needs private_key (kv:// or env://)")
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        connect_args["private_key"] = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption())
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True, pool_recycle=3000)


def _table(src: SourceConfig, name: str, *cols: str) -> sa.TableClause:
    parts = name.split(".")
    if len(parts) == 3:
        db, schema, table = parts
        if src.database and db.upper() != src.database.upper():
            raise ValueError(f"table {name} is outside the source database {src.database}")
    elif len(parts) == 2:
        schema, table = parts
    else:
        schema, table = None, parts[0]
    return sa.table(table, *(sa.column(c) for c in dict.fromkeys(cols)), schema=schema)


def _to_utc(value: Any, tz: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(UTC).replace(tzinfo=None)


def _from_utc(value: datetime, tz: ZoneInfo) -> datetime:
    """Naive UTC → naive wall time in the data's timezone (what TIMESTAMP_NTZ columns hold)."""
    return value.replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)


# ── reading feeds ────────────────────────────────────────────────────────────


class FeedSource:
    def __init__(self, src: SourceConfig):
        self.src = src
        self.engine = feed_engine(src)
        self.tz = ZoneInfo(src.data_timezone or "UTC")
        self.dialect = self.engine.dialect.name

    def _ts(self, feed: FeedConfig, t: sa.TableClause) -> sa.ColumnElement:
        col = t.c[feed.timestamp]
        if feed.timestamp_cast == "try_to_timestamp" and self.dialect == "snowflake":
            return sa.func.try_to_timestamp(col)
        return col

    def _where(self, feed: FeedConfig, t: sa.TableClause) -> list[sa.ColumnElement]:
        conds: list[sa.ColumnElement] = []
        if feed.filter:
            col = t.c[feed.filter.column]
            if feed.filter.include:
                conds.append(sa.or_(*(col.ilike(p) for p in feed.filter.include)))
            conds += [sa.not_(col.ilike(p)) for p in feed.filter.exclude]
        return conds

    def _t(self, feed: FeedConfig) -> sa.TableClause:
        cols = [feed.timestamp, *(feed.key), *(feed.required_fields)]
        if feed.filter:
            cols.append(feed.filter.column)
        return _table(self.src, feed.table, *cols)

    def _bind(self, value: datetime) -> Any:
        local = _from_utc(value, self.tz)
        return local.isoformat(sep=" ") if self.dialect == "sqlite" else local

    def minute_counts(self, feed: FeedConfig, since: datetime, until: datetime) -> list[tuple[datetime, int]]:
        """Rows per minute in [since, until), as (naive UTC minute, count). Cheap: one grouped scan."""
        t = self._t(feed)
        ts = self._ts(feed, t)
        bucket = sa.func.strftime("%Y-%m-%d %H:%M:00", ts) if self.dialect == "sqlite" else sa.func.date_trunc("MINUTE", ts)
        stmt = (
            sa.select(bucket.label("m"), sa.func.count().label("n"))
            .where(ts >= self._bind(since), ts < self._bind(until), *self._where(feed, t))
            .group_by(bucket)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        out = []
        for m, n in rows:
            utc = _to_utc(m, self.tz)
            if utc is not None:
                out.append((utc, int(n)))
        return sorted(out)

    def window_metrics(self, feed: FeedConfig, start: datetime, end: datetime) -> WindowMetrics:
        t = self._t(feed)
        ts = self._ts(feed, t)
        conds = [ts >= self._bind(start), ts < self._bind(end), *self._where(feed, t)]
        counts = [sa.func.count().label("n")]
        counts += [sa.func.sum(sa.case((t.c[f].is_(None), 1), (sa.cast(t.c[f], sa.String) == "", 1), else_=0)) for f in feed.required_fields]
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(*counts).where(*conds)).one()
            dupes = 0
            if feed.key:
                g = sa.select(sa.func.count().label("k")).where(*conds).group_by(*(t.c[k] for k in feed.key)).subquery()
                dupes = int(conn.execute(sa.select(sa.func.coalesce(sa.func.sum(g.c.k - 1), 0))).scalar() or 0)
        n = int(row[0] or 0)
        rates = {f: (float(row[i + 1] or 0) / n if n else 0.0) for i, f in enumerate(feed.required_fields)}
        return WindowMetrics(row_count=n, null_rates=rates, duplicate_rows=dupes)

    def last_loaded(self, feed: FeedConfig, since: datetime) -> datetime | None:
        t = self._t(feed)
        ts = self._ts(feed, t)
        with self.engine.connect() as conn:
            v = conn.execute(sa.select(sa.func.max(ts)).where(ts >= self._bind(since), *self._where(feed, t))).scalar()
        return _to_utc(v, self.tz)

    def read_schema(self, table: str) -> list[ColumnInfo]:
        t = _table(self.src, table)
        cols = sa.inspect(self.engine).get_columns(t.name, schema=t.schema)
        return [ColumnInfo(name=c["name"].upper(), type=str(c["type"]).upper()) for c in cols]


# ── producers ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProducerEvent:
    at: datetime
    status: str  # SUCCEEDED | FAILED | SKIPPED | CANCELLED | RUNNING
    message: str = ""
    name: str = ""


class FeedProducers:
    """Producer health per feed. Also the project's `pipeline` connector, so the investigator's
    get_activity_runs tool returns producer evidence for a feed run."""

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self._http = httpx.Client(timeout=20)

    def events(self, feed: FeedConfig, since: datetime) -> list[ProducerEvent]:
        p = feed.producer
        try:
            if p.type == "snowflake_task" and p.name:
                return self._task_history(feed, p.name, since)
            if p.type == "snowflake_stream" and p.task:
                return self._task_history(feed, p.task, since)
            if p.type == "azure_function" and p.app_insights_app_id:
                return self._function_failures(p.name, p.app_insights_app_id, since)
        except Exception as exc:  # noqa: BLE001 - producer health is evidence, never a crash
            log.warning("producer check for %s failed: %s", feed.id, exc)
            raise ProducerUnavailable(str(exc)) from exc
        return []

    def _task_history(self, feed: FeedConfig, task: str, since: datetime) -> list[ProducerEvent]:  # pragma: no cover - Snowflake
        db, schema, name = (task.split(".") + ["", ""])[:3] if task.count(".") == 2 else ("", "", task)
        src = self.cfg.source(feed.source)
        engine = feed_engine(src)
        if engine.dialect.name != "snowflake":
            return []
        fn = f"{db}.INFORMATION_SCHEMA.TASK_HISTORY" if db else "INFORMATION_SCHEMA.TASK_HISTORY"
        stmt = sa.text(
            f"SELECT SCHEDULED_TIME, COMPLETED_TIME, STATE, ERROR_MESSAGE, NAME FROM TABLE({fn}("  # fn is a validated identifier
            "SCHEDULED_TIME_RANGE_START => :since, TASK_NAME => :name, RESULT_LIMIT => 200)) ORDER BY SCHEDULED_TIME DESC"
        )
        with engine.connect() as conn:
            rows = conn.execute(stmt, {"since": since.replace(tzinfo=UTC), "name": name.upper()}).all()
        return [ProducerEvent(at=_to_utc(r[1] or r[0], ZoneInfo("UTC")) or since, status=str(r[2]), message=r[3] or "", name=f"{schema}.{name}")
                for r in rows]

    def _function_failures(self, app: str, app_id: str, since: datetime) -> list[ProducerEvent]:  # pragma: no cover - Azure
        from azure.identity import DefaultAzureCredential

        token = DefaultAzureCredential().get_token("https://api.applicationinsights.io/.default").token
        query = (
            f"requests | where timestamp > datetime({since.isoformat()}) "
            "| project timestamp, success, resultCode, operation_Name, cloud_RoleName "
            "| order by timestamp desc | take 200"
        )
        r = self._http.post(f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
                            headers={"Authorization": f"Bearer {token}"}, json={"query": query})
        r.raise_for_status()
        table = r.json()["tables"][0]
        out = []
        for ts, success, code, op, role in table["rows"]:
            at = _to_utc(ts, ZoneInfo("UTC")) or since
            out.append(ProducerEvent(at=at, status="SUCCEEDED" if success else "FAILED", message=f"{op} → {code}", name=role or app))
        return out

    # Pipeline protocol (used by the investigator's tools) ------------------------------

    def get_runs(self, run_ids: list[str]) -> dict[str, PipelineRun]:
        return {}

    def get_activity_runs(self, run_id: str) -> list[ActivityRun]:
        """For a feed run id `<feed>@<iso slot>`: the producer's events around that slot."""
        feed_id, _, iso = run_id.partition("@")
        feed = next((f for f in self.cfg.feeds if f.id == feed_id), None)
        if feed is None or not iso:
            return []
        slot = datetime.fromisoformat(iso)
        try:
            events = self.events(feed, slot - timedelta(hours=2))
        except ProducerUnavailable:
            return []
        near = [e for e in events if slot - timedelta(hours=2) <= e.at <= slot + timedelta(hours=6)]
        return [
            ActivityRun(run_id=run_id, name=e.name or feed.producer.name, type=feed.producer.type, status=e.status.title(),
                        started_at=e.at, ended_at=e.at, duration_s=None, output={"message": e.message},
                        error={"message": e.message} if e.status.upper() == "FAILED" else None)
            for e in near
        ]


class ProducerUnavailable(RuntimeError):
    pass
