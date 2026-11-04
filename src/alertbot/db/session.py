"""Engine and session factory for the metadata DB."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from ..settings import get_settings
from .models import Base


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")  # API reads while the bot writes
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800)


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().db_url)


@lru_cache
def _factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = _factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = _factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    add_missing_columns(engine)


def add_missing_columns(engine: Engine) -> None:
    """New nullable (or defaulted) columns on existing tables: add them in place, so an upgrade keeps every row."""
    have = inspect(engine)
    tables = set(have.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue
            existing = {c["name"] for c in have.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing or (not col.nullable and col.server_default is None):
                    continue
                kind = col.type.compile(dialect=engine.dialect)
                default = f" DEFAULT {col.server_default.arg}" if col.server_default is not None else ""
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD "{col.name}" {kind}{default}'))


def drop_all() -> None:
    Base.metadata.drop_all(get_engine())
