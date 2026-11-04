"""SQL data source for Azure SQL (production) and SQLite (prototype estate).

Guardrail: every statement is built from SQLAlchemy Core constructs over
identifiers taken from validated project config, with values bound as
parameters. Nothing here ever executes free-form SQL.
"""

from __future__ import annotations

import struct
from datetime import datetime
from functools import lru_cache

import sqlalchemy as sa
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL

from ..config import OutputConfig, SourceConfig, TriggerTableConfig
from ..timeutil import to_naive_utc
from .base import ColumnInfo, TriggerRow, WindowMetrics

_SQL_COPT_SS_ACCESS_TOKEN = 1256


def _split(name: str) -> tuple[str | None, str]:
    schema, _, table = name.rpartition(".")
    return (schema or None), table


def _table(name: str, *columns: str, datetimes: tuple[str, ...] = ()) -> sa.TableClause:
    """Lightweight table construct; datetime columns are typed so values bind and parse portably."""
    schema, table = _split(name)
    cols = [sa.column(c, sa.DateTime()) if c in datetimes else sa.column(c) for c in dict.fromkeys(columns)]
    return sa.table(table, *cols, schema=schema)


def _as_dt(value) -> datetime | None:  # noqa: ANN001
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return to_naive_utc(value)


def _azure_sql_engine(src: SourceConfig) -> Engine:  # pragma: no cover - needs Azure
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    credential = ManagedIdentityCredential() if src.auth == "managed_identity" else DefaultAzureCredential()
    url = src.url or URL.create(
        "mssql+pyodbc",
        host=src.server,
        database=src.database,
        query={"driver": "ODBC Driver 18 for SQL Server", "Encrypt": "yes", "ApplicationIntent": "ReadOnly"},
    )
    engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)

    @event.listens_for(engine, "do_connect")
    def _token(_dialect, _rec, cargs, cparams):  # noqa: ANN001
        # Entra token auth: drop the implicit Windows auth flag and pass an access token.
        cargs[0] = cargs[0].replace(";Trusted_Connection=Yes", "")
        token = credential.get_token("https://database.windows.net/.default").token.encode("utf-16-le")
        cparams["attrs_before"] = {_SQL_COPT_SS_ACCESS_TOKEN: struct.pack(f"<I{len(token)}s", len(token), token)}

    return engine


@lru_cache(maxsize=32)
def engine_for(src: SourceConfig) -> Engine:
    if src.type == "sqlite":
        return create_engine(src.url, connect_args={"check_same_thread": False})
    return _azure_sql_engine(src)


class SqlDataSource:
    def __init__(self, src: SourceConfig):
        self.config = src
        self.engine = engine_for(src)

    # Trigger table ---------------------------------------------------------

    def fetch_trigger_rows(self, cfg: TriggerTableConfig, since: datetime) -> list[TriggerRow]:
        t = _table(
            cfg.table,
            cfg.id_column, cfg.site_column, cfg.status_column, cfg.run_id_column, cfg.created_column,
            datetimes=(cfg.created_column,),
        )
        c = t.c
        stmt = (
            sa.select(c[cfg.id_column], c[cfg.site_column], c[cfg.status_column], c[cfg.run_id_column], c[cfg.created_column])
            .where(c[cfg.created_column] >= since)
            .order_by(c[cfg.created_column])
        )
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [
            TriggerRow(
                trigger_id=str(r[0]),
                site=str(r[1]),
                raw_status=str(r[2] or ""),
                state=cfg.statuses.state_of(r[2]),
                run_id=(str(r[3]) if r[3] else None),
                requested_at=_as_dt(r[4]),
            )
            for r in rows
        ]

    # Scraped output --------------------------------------------------------

    def window_metrics(self, output: OutputConfig, site: str, start: datetime, end: datetime) -> WindowMetrics:
        cols = [output.group_by, output.loaded_at, *output.key, *output.required_fields]
        t = _table(output.table, *cols, datetimes=(output.loaded_at,))
        c = t.c
        in_window = sa.and_(c[output.group_by] == site, c[output.loaded_at] >= start, c[output.loaded_at] <= end)

        counts = [sa.func.count().label("n")]
        counts += [
            sa.func.sum(sa.case((c[f].is_(None), 1), (sa.cast(c[f], sa.String) == "", 1), else_=0)).label(f"null_{i}")
            for i, f in enumerate(output.required_fields)
        ]
        groups = sa.select(sa.func.count().label("k")).where(in_window).group_by(*(c[k] for k in output.key)).subquery()
        dup_stmt = sa.select(sa.func.coalesce(sa.func.sum(groups.c.k - 1), 0))

        with self.engine.connect() as conn:
            row = conn.execute(sa.select(*counts).where(in_window)).one()
            dupes = int(conn.execute(dup_stmt).scalar() or 0)

        n = int(row[0] or 0)
        null_rates = {f: (float(row[i + 1] or 0) / n if n else 0.0) for i, f in enumerate(output.required_fields)}
        return WindowMetrics(row_count=n, null_rates=null_rates, duplicate_rows=dupes)

    def rows_by_site(self, output: OutputConfig, start: datetime, end: datetime) -> dict[str, int]:
        t = _table(output.table, output.group_by, output.loaded_at, datetimes=(output.loaded_at,))
        c = t.c
        stmt = (
            sa.select(c[output.group_by], sa.func.count())
            .where(c[output.loaded_at] >= start, c[output.loaded_at] <= end)
            .group_by(c[output.group_by])
        )
        with self.engine.connect() as conn:
            return {str(site): int(n) for site, n in conn.execute(stmt)}

    def rows_outside_windows(
        self, output: OutputConfig, site: str, start: datetime, end: datetime, windows: list[tuple[datetime, datetime]]
    ) -> int:
        """Rows for `site` loaded in [start, end] that fall in none of the given run windows."""
        t = _table(output.table, output.group_by, output.loaded_at, datetimes=(output.loaded_at,))
        c = t.c
        loaded = c[output.loaded_at]
        conds = [c[output.group_by] == site, loaded >= start, loaded <= end]
        if windows:
            conds.append(sa.not_(sa.or_(*(sa.and_(loaded >= ws, loaded <= we) for ws, we in windows))))
        with self.engine.connect() as conn:
            return int(conn.execute(sa.select(sa.func.count()).where(*conds)).scalar() or 0)

    def last_loaded_by_site(self, output: OutputConfig, since: datetime) -> dict[str, datetime]:
        t = _table(output.table, output.group_by, output.loaded_at, datetimes=(output.loaded_at,))
        c = t.c
        stmt = (
            sa.select(c[output.group_by], sa.func.max(c[output.loaded_at]))
            .where(c[output.loaded_at] >= since)
            .group_by(c[output.group_by])
        )
        with self.engine.connect() as conn:
            return {str(site): _as_dt(ts) for site, ts in conn.execute(stmt) if ts is not None}

    def read_schema(self, output: OutputConfig) -> list[ColumnInfo]:
        schema, table = _split(output.table)
        cols = sa.inspect(self.engine).get_columns(table, schema=schema)
        return [ColumnInfo(name=col["name"], type=str(col["type"]).upper()) for col in cols]
