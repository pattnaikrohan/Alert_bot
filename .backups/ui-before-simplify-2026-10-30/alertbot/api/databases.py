"""Databases people connect, and which pipeline loads which table (the mapping), for data-quality checks.

A connection is personal: only its owner sees or edits it, and its password / key is write-only (stored encrypted,
never returned). Mappings belong to a project: any member can see them; a member maps a table on a database they
connected themselves. Every check result is aggregate-only (see alertbot.dq.privacy).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db import BotState, DbConnection, DqMapping, Project, UserProject, WebSession
from ..dq.client import DbError
from ..dq.privacy import RawDataRejected, safe_name
from ..dq.service import (
    MappingSettings,
    PlanError,
    build_plan,
    client_of,
    connection_view,
    mapping_view,
    mappings_of,
    reconnect_all,
    resolve_table,
    save_connection,
    suggestions,
    test_connection,
)
from ..dq.vault import VaultError
from ..settings import get_settings
from ..timeutil import iso, utcnow
from .auth import AccessDep, SessionDep, UserDep

router = APIRouter()

_HOST = re.compile(r"^[A-Za-z0-9._\-:\\]{1,255}$")


class ConnIn(BaseModel):
    kind: Literal["sqlserver", "snowflake", "sqlite"]
    name: str = Field(default="", max_length=120)
    host: str
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str
    auth: Literal["sql", "entra", "password", "keypair"]
    username: str = Field(default="", max_length=200)
    secret: str = Field(default="", max_length=20000)  # write-only
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip()
        if not _HOST.match(v):
            raise ValueError("a server name, IP address or Snowflake account (letters, digits, . - : only)")
        return v

    @field_validator("database")
    @classmethod
    def _db(cls, v: str) -> str:
        return safe_name(v.strip())


class ConnStringIn(BaseModel):
    connection_string: str = Field(min_length=1, max_length=4000)  # write-only: parsed, never stored or echoed
    name: str = Field(default="", max_length=120)


def _parsed(body: ConnStringIn) -> ConnIn:
    """A pasted connection string as a connection. Errors name keys only, never values."""
    from pydantic import ValidationError

    from ..dq.connstr import ConnStringError, parse

    try:
        p = parse(body.connection_string)
    except ConnStringError as exc:
        raise HTTPException(422, f"Couldn't read the connection string: {exc}") from exc
    try:
        return ConnIn.model_validate({**p.fields(), "name": body.name})
    except ValidationError as exc:
        err = exc.errors()[0]
        raise HTTPException(422, f"Couldn't read the connection string: {'.'.join(map(str, err['loc']))}: {err['msg']}") from exc


class ConnPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=200)
    secret: str = Field(default="", max_length=20000)
    options: dict[str, Any] | None = None


class MappingIn(BaseModel):
    pipeline: str = Field(min_length=1, max_length=200)
    connection_id: int
    schema_name: str = Field(alias="schema", min_length=1, max_length=128)
    table: str = Field(min_length=1, max_length=128)
    settings: dict[str, Any] = Field(default_factory=dict)


class MappingPatch(BaseModel):
    pipeline: str | None = Field(default=None, min_length=1, max_length=200)
    settings: dict[str, Any] | None = None
    enabled: bool | None = None
    follow_azure: bool | None = None  # False: keep checking even though Azure's definitions don't show this pipeline loading it


def _testing() -> bool:
    import os

    return "PYTEST_CURRENT_TEST" in os.environ


def _check_kind(body: ConnIn) -> None:
    if body.kind == "sqlite" and get_settings().auth_mode != "dev":
        raise HTTPException(422, "unsupported database type")
    allowed = {"sqlserver": {"sql", "entra"}, "snowflake": {"password", "keypair"}, "sqlite": {"sql"}}[body.kind]
    if body.auth not in allowed:
        raise HTTPException(422, f"{body.kind} connects with {' or '.join(sorted(allowed))}")
    if body.auth in ("sql", "password", "keypair") and body.kind != "sqlite" and not body.username:
        raise HTTPException(422, "a user name is needed")


def _mine(session: SessionDep, user: UserDep, conn_id: int) -> DbConnection:
    c = session.get(DbConnection, conn_id)
    if c is None or c.user_id != user.id:
        raise HTTPException(404, "database not found")
    return c


def _projects(session: SessionDep, user: UserDep) -> list[Project]:
    return list(session.scalars(select(Project).join(UserProject, UserProject.project_id == Project.id)
                                .where(UserProject.user_id == user.id, Project.active.is_(True)).order_by(Project.display_name)))


def _home_account(session: SessionDep, user: UserDep) -> str | None:
    return session.scalar(select(WebSession.azure_account).where(WebSession.user_id == user.id, WebSession.azure_account.is_not(None))
                          .order_by(WebSession.last_seen_at.desc()).limit(1))


def _used_by(session: SessionDep, conn: DbConnection) -> list[dict[str, Any]]:
    rows = session.execute(select(DqMapping, Project).join(Project, Project.id == DqMapping.project_id).where(DqMapping.connection_id == conn.id))
    return [{"project": p.key, "project_name": p.display_name, "pipeline": m.pipeline, "schema": m.schema_name, "table": m.table_name,
             "mapping_id": m.id, "enabled": m.enabled, "automatic": m.created_by is None, "retired": m.retired_at is not None,
             "retired_reason": m.retired_reason, "retired_at": iso(m.retired_at)}
            for m, p in sorted(rows, key=lambda r: (r[0].retired_at is not None, r[1].display_name, r[0].pipeline, r[0].table_name))]


# ── connections ──


@router.get("/api/databases")
def list_databases(user: UserDep, session: SessionDep) -> dict:
    """My databases (newest use first), what each feeds, and tables my pipelines load that I could map."""
    conns = session.scalars(select(DbConnection).where(DbConnection.user_id == user.id)
                            .order_by(DbConnection.last_used_at.desc().nullslast(), DbConnection.created_at.desc()))
    return {"connections": [connection_view(c, _used_by(session, c)) for c in conns],
            "suggestions": suggestions(session, user, _projects(session, user))}


@router.post("/api/databases/parse")
def parse_connection_string(body: ConnStringIn, user: UserDep) -> dict:
    """What a pasted connection string says (server, database, user, options), without the password. Nothing is stored."""
    from ..dq.connstr import parse

    p = _parsed(body)
    return {**parse(body.connection_string).preview(), "kind": p.kind, "host": p.host, "database": p.database}


@router.post("/api/databases/from-string")
def add_database_from_string(body: ConnStringIn, user: UserDep, session: SessionDep) -> dict:
    """Connect a database from a pasted connection string: parsed into its parts (the password encrypted), then tested.
    The bot's own safety settings (read-only intent, time limits, encryption unless the string turns it off) still apply."""
    return add_database(_parsed(body), user, session)


@router.post("/api/databases")
def add_database(body: ConnIn, user: UserDep, session: SessionDep) -> dict:
    """Connect a database: it's tested first, and kept only if the bot can sign in and read its table list."""
    _check_kind(body)
    if body.auth != "entra" and not body.secret and body.kind != "sqlite":
        raise HTTPException(422, "the password (or private key) is needed")
    existing = session.scalars(select(DbConnection).where(DbConnection.user_id == user.id, DbConnection.kind == body.kind,
                                                          DbConnection.host == body.host, DbConnection.database == body.database,
                                                          DbConnection.username == body.username)).first()
    data = body.model_dump()
    data["name"] = body.name.strip() or f"{body.database} on {body.host}"
    if body.auth == "entra":
        data["home_account_id"] = _home_account(session, user)
        if not data["home_account_id"]:
            raise HTTPException(409, "sign in with Microsoft again to use your Microsoft account for this database")
    try:
        c = save_connection(session, user, data, existing)
    except (VaultError, IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc
    result = test_connection(session, c)
    if not result["ok"]:
        if existing is None:
            session.delete(c)
            session.flush()
        raise HTTPException(400, result["error"] or "couldn't connect")
    return {"connection": connection_view(c, _used_by(session, c)), "can_write": result["can_write"],
            "suggestions": [s for s in suggestions(session, user, _projects(session, user)) if s["connection_id"] == c.id]}


@router.patch("/api/databases/{conn_id}")
def edit_database(conn_id: Annotated[int, Path()], body: ConnPatch, user: UserDep, session: SessionDep) -> dict:
    c = _mine(session, user, conn_id)
    save_connection(session, user, {k: v for k, v in body.model_dump().items() if v not in (None, "")}, c)
    result = test_connection(session, c)
    return {"connection": connection_view(c, _used_by(session, c)), **result}


@router.delete("/api/databases/{conn_id}")
def remove_database(conn_id: Annotated[int, Path()], user: UserDep, session: SessionDep) -> dict:
    """Disconnect: the stored credential is deleted, and so are the mappings that used it."""
    from ..dq.client import forget

    c = _mine(session, user, conn_id)
    n = len(_used_by(session, c))
    for m in session.scalars(select(DqMapping).where(DqMapping.connection_id == c.id)):
        session.delete(m)
    session.delete(c)
    session.flush()
    forget(conn_id)
    return {"removed": conn_id, "mappings_removed": n}


@router.post("/api/databases/{conn_id}/test")
def test_database(conn_id: Annotated[int, Path()], user: UserDep, session: SessionDep) -> dict:
    c = _mine(session, user, conn_id)
    return {**test_connection(session, c), "connection": connection_view(c, _used_by(session, c))}


@router.post("/api/databases/reconnect")
def reconnect(user: UserDep, session: SessionDep) -> dict:
    """On sign-in: reconnect every database this person connected before (once per session is plenty)."""
    return {"connections": reconnect_all(session, user)}


@router.get("/api/databases/{conn_id}/tables")
def database_tables(conn_id: Annotated[int, Path()], user: UserDep, session: SessionDep) -> dict:
    """The tables and views this login can see (names only), to pick from when mapping."""
    c = _mine(session, user, conn_id)
    try:
        tables = client_of(c).tables()
    except (DbError, VaultError) as exc:
        raise HTTPException(502, str(exc)) from exc
    c.last_used_at = utcnow()
    return {"tables": tables}


@router.get("/api/databases/{conn_id}/columns")
def database_columns(conn_id: Annotated[int, Path()], schema: str, table: str, user: UserDep, session: SessionDep) -> dict:
    c = _mine(session, user, conn_id)
    try:
        client = client_of(c)
        schema, table = resolve_table(client, schema, table)
        cols = client.columns(schema, table)
    except (DbError, VaultError, RawDataRejected) as exc:
        raise HTTPException(502, str(exc)) from exc
    kinds = {n: client.dialect.kind_of(t) for n, t in cols}
    return {"schema": schema, "table": table, "columns": [{"name": n, "type": t, "kind": kinds[n]} for n, t in cols]}


# ── mappings (per project) ──


def check_soon(project_key: str) -> None:
    """Check a project's newly mapped / changed tables now, in the background (nobody has to press anything)."""
    import threading

    from ..config import load_projects
    from ..engine.cycle import run_cycle

    cfg = load_projects().get(project_key)
    if cfg is None:
        return

    def go() -> None:
        import contextlib

        with contextlib.suppress(Exception):  # logged by the cycle; the next scheduled cycle tries again
            run_cycle(cfg)  # the new mapping is due ("first check"); a changed one is due too

    threading.Thread(target=go, name=f"dq-check-{project_key}", daemon=True).start()


def _pipelines(session: SessionDep, project: Project) -> list[str]:
    inv = session.get(BotState, f"inventory:{project.key}")
    return sorted({i["name"] for i in ((inv.value or {}).get("items") or [])} if inv else set())


@router.get("/api/projects/{key}/mappings")
def project_mappings(access: AccessDep, session: SessionDep) -> dict:
    """Which pipeline loads which table here; suggestions from lineage for my databases; the pipelines to pick from."""
    mine = list(session.scalars(select(DbConnection).where(DbConnection.user_id == access.user.id)))
    return {"mappings": [mapping_view(m) for m in mappings_of(session, access.project)],
            "suggestions": suggestions(session, access.user, [access.project]),
            "pipelines": _pipelines(session, access.project),
            "connections": [connection_view(c) for c in mine], "me": access.user.id}


def _validate(session: SessionDep, m: DqMapping) -> None:
    try:
        MappingSettings.model_validate(m.settings or {})
        build_plan(client_of(m.connection), m)
    except PlanError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:  # pydantic
        raise HTTPException(422, str(exc).splitlines()[0][:300]) from exc
    except (DbError, VaultError, RawDataRejected) as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/api/projects/{key}/mappings")
def add_mapping(body: MappingIn, access: AccessDep, session: SessionDep) -> dict:
    c = session.get(DbConnection, body.connection_id)
    if c is None or c.user_id != access.user.id:
        raise HTTPException(404, "database not found (map tables on a database you connected)")
    pipes = _pipelines(session, access.project)
    if pipes and body.pipeline not in pipes:
        raise HTTPException(422, f"{body.pipeline} isn't a pipeline of this project")
    try:
        schema, table = resolve_table(client_of(c), body.schema_name, body.table)
    except (DbError, VaultError) as exc:
        raise HTTPException(502, str(exc)) from exc
    m = DqMapping(project_id=access.project.id, connection_id=c.id, pipeline=body.pipeline, schema_name=schema, table_name=table,
                  settings=body.settings, created_by=access.user.id)
    from ..dq.service import canonical, set_dismissed

    set_dismissed(session, access.project.key, f"{body.pipeline}|{canonical(c, schema, table)}", False)  # mapped by hand again
    m.connection = c
    _validate(session, m)
    session.add(m)
    try:
        session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, f"{body.pipeline} → {schema}.{table} is already mapped") from exc
    session.commit()
    if not _testing():
        check_soon(access.project.key)
    return {"mapping": mapping_view(m), "checking": True}


def _mapping(session: SessionDep, access: AccessDep, mapping_id: int) -> DqMapping:
    m = session.get(DqMapping, mapping_id)
    if m is None or m.project_id != access.project.id:
        raise HTTPException(404, "mapping not found")
    return m


@router.patch("/api/projects/{key}/mappings/{mapping_id}")
def edit_mapping(mapping_id: Annotated[int, Path()], body: MappingPatch, access: AccessDep, session: SessionDep) -> dict:
    m = _mapping(session, access, mapping_id)
    if body.pipeline is not None:
        m.pipeline = body.pipeline
    if body.enabled is not None:
        m.enabled = body.enabled
    if body.follow_azure is not None:
        m.follow_azure = body.follow_azure
        if not body.follow_azure and m.retired_by != "database":
            m.retired_at, m.retired_reason, m.retired_by = None, None, None
    if body.settings is not None:
        m.settings = body.settings
        _validate(session, m)
    m.updated_at = utcnow()
    session.flush()
    if body.settings is not None or body.enabled or body.follow_azure is False:
        session.commit()
        if not _testing():
            check_soon(access.project.key)
    return {"mapping": mapping_view(m)}


@router.delete("/api/projects/{key}/mappings/{mapping_id}")
def remove_mapping(mapping_id: Annotated[int, Path()], access: AccessDep, session: SessionDep) -> dict:
    m = _mapping(session, access, mapping_id)
    if not access.is_owner and m.created_by not in (None, access.user.id) and m.connection.user_id != access.user.id:
        raise HTTPException(403, "only the person who mapped it, the database's owner or a project owner can remove it")
    from ..dq.service import canonical, set_dismissed

    set_dismissed(session, access.project.key, f"{m.pipeline}|{canonical(m.connection, m.schema_name, m.table_name)}", True)
    session.delete(m)
    session.flush()
    return {"removed": mapping_id}
