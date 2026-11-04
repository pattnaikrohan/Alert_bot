"""Warehouse cost: unused tables on the databases you connected, what they cost to keep, and the script to remove them.

    GET  /api/storage?days=180              the report (from the latest scans; the worker scans hourly)
    POST /api/storage/scan                  scan my databases now
    PUT  /api/storage/{conn}/price          my storage price per GB-month for that database (null: the list price)
    PUT  /api/storage/{conn}/keep           keep a table (never suggested again), or stop keeping it
    POST /api/storage/{conn}/script         the drop (or archive) script for chosen tables, views, procedures, functions
    PUT  /api/storage/pipelines/{p}/keep    keep a pipeline (never suggested again), or stop keeping it
    POST /api/storage/pipelines/{p}/script  Azure CLI commands to back up, stop and delete chosen pipelines
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import load_projects
from ..db import DbConnection, Project, session_scope
from ..dq import idle
from ..dq import storage as S
from ..timeutil import iso, utcnow
from .auth import SessionDep, UserDep

router = APIRouter()


def _mine(session: SessionDep, user: UserDep, conn_id: int) -> DbConnection:
    c = session.get(DbConnection, conn_id)
    if c is None or c.user_id != user.id:
        raise HTTPException(404, "database not found")
    return c


@router.get("/api/storage")
def storage_report(user: UserDep, session: SessionDep, days: Annotated[int, Query(ge=30, le=1095)] = 180,
                   conn: Annotated[int | None, Query(ge=1)] = None) -> dict:
    return S.report(session, user, days, conn=conn)


@router.post("/api/storage/scan")
def storage_scan(user: UserDep, session: SessionDep, days: Annotated[int, Query(ge=30, le=1095)] = 180,
                 conn: Annotated[int | None, Query(ge=1)] = None) -> dict:
    ids = [i for i in session.scalars(select(DbConnection.id).where(DbConnection.user_id == user.id)) if conn is None or i == conn]
    session.commit()  # each database is read with nothing held, then recorded in its own short step
    for cid in ids:
        with session_scope() as s:
            c = s.get(DbConnection, cid)
            if c is not None:
                S.scan(s, c)
    session.expire_all()
    return S.report(session, user, days, conn=conn)


class PriceIn(BaseModel):
    price: float | None = Field(default=None, ge=0, le=1000)  # $ per GB-month; null → the list price


@router.put("/api/storage/{conn_id}/price")
def storage_price(conn_id: Annotated[int, Path()], body: PriceIn, user: UserDep, session: SessionDep) -> dict:
    c = _mine(session, user, conn_id)
    st = S.state_of(session, c.id)
    st["price"] = body.price
    S.save_state(session, c.id, st)
    return {"price": S.price_of(c, st)[0]}


class KeepIn(BaseModel):
    key: str = Field(min_length=3, max_length=300)  # SCHEMA.TABLE as the scan lists it
    keep: bool = True
    reason: str | None = Field(default=None, max_length=300)


@router.put("/api/storage/{conn_id}/keep")
def storage_keep(conn_id: Annotated[int, Path()], body: KeepIn, user: UserDep, session: SessionDep) -> dict:
    c = _mine(session, user, conn_id)
    st = S.state_of(session, c.id)
    if body.key not in (st.get("tables") or {}) and body.key not in (st.get("objects") or {}):
        raise HTTPException(404, "that isn't in the latest scan")
    keep = dict(st.get("keep") or {})
    if body.keep:
        keep[body.key] = {"by": user.name, "at": iso(utcnow()), "reason": (body.reason or "").strip() or None}
    else:
        keep.pop(body.key, None)
    st["keep"] = keep
    S.save_state(session, c.id, st)
    return {"kept": sorted(keep)}


class ScriptIn(BaseModel):
    keys: list[Annotated[str, Field(min_length=3, max_length=300)]] = Field(min_length=1, max_length=5000)
    mode: Literal["drop", "archive"] = "drop"


@router.post("/api/storage/{conn_id}/script")
def storage_script(conn_id: Annotated[int, Path()], body: ScriptIn, user: UserDep, session: SessionDep) -> dict:
    """Only tables from the latest scan; ones the pipelines use are refused (a person must stop that use first)."""
    c = _mine(session, user, conn_id)
    st = S.state_of(session, c.id)
    if not st.get("tables"):
        raise HTTPException(409, "scan this database first")
    guard = S.protected(session, c)
    used = [k for k in body.keys if guard.get(k.upper())]
    keys = [k for k in body.keys if k not in used]
    out = S.script(c, st, keys, body.mode, user.name, price=S.price_of(c, st)[0])
    out["skipped"] = [f"{k}: used by {', '.join(guard[k.upper()])}" for k in used] + out["skipped"]
    return out


# ── pipelines nobody runs (Data Factory / Synapse / Logic Apps) ──────────────


def _my_project(session: SessionDep, user: UserDep, key: str) -> tuple[Project, Any]:
    p = next((x for x in idle.my_projects(session, user) if x.key == key), None)
    cfg = load_projects().get(key)
    if p is None or cfg is None or cfg.pipeline is None:
        raise HTTPException(404, "project not found")
    return p, cfg


class PipeKeepIn(BaseModel):
    name: str = Field(min_length=1, max_length=260)
    keep: bool = True
    reason: str | None = Field(default=None, max_length=300)


@router.put("/api/storage/pipelines/{project}/keep")
def pipeline_keep(project: Annotated[str, Path(max_length=120)], body: PipeKeepIn, user: UserDep, session: SessionDep) -> dict:
    p, _ = _my_project(session, user, project)
    keep = idle.keep_of(session, p.key)
    if body.keep:
        keep[body.name] = {"by": user.name, "at": iso(utcnow()), "reason": (body.reason or "").strip() or None}
    else:
        keep.pop(body.name, None)
    idle.save_keep(session, p.key, keep)
    return {"kept": sorted(keep)}


class PipeScriptIn(BaseModel):
    names: list[Annotated[str, Field(min_length=1, max_length=260)]] = Field(min_length=1, max_length=2000)
    mode: Literal["delete", "stop"] = "delete"


@router.post("/api/storage/pipelines/{project}/script")
def pipeline_script(project: Annotated[str, Path(max_length=120)], body: PipeScriptIn, user: UserDep, session: SessionDep,
                    days: Annotated[int, Query(ge=30, le=1095)] = 180) -> dict:
    """Azure CLI commands to back up, stop and delete the chosen pipelines. Ones in use or kept are left out."""
    p, cfg = _my_project(session, user, project)
    rep = S.report(session, user, days)
    rows = next((x["rows"] for x in rep["pipelines"]["projects"] if x["key"] == p.key), [])
    return idle.script(p, cfg, rows, body.names, body.mode, user.name)
