"""Snowflake compute cost: the accounts you connected, what they spend, on what, what's wasted, and alerts.

    GET  /api/compute                       every Snowflake account I connected: spend, warehouses, who, queries, pipelines, savings
    POST /api/compute/scan                  read my accounts now
    PUT  /api/compute/{conn}/settings       price per credit, budgets, alert thresholds, the pipelines' logins
    POST /api/compute/{conn}/script         the ALTER / resource-monitor script for chosen savings (the bot never runs it)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..db import DbConnection, session_scope
from ..dq import compute as C
from ..timeutil import utcnow
from .auth import SessionDep, UserDep

router = APIRouter()


def _mine(session: SessionDep, user: UserDep, conn_id: int) -> DbConnection:
    c = session.get(DbConnection, conn_id)
    if c is None or c.user_id != user.id or c.kind != "snowflake":
        raise HTTPException(404, "Snowflake account not found")
    return c


@router.get("/api/compute")
def compute_report(user: UserDep, session: SessionDep) -> dict:
    return C.report(session, user)


@router.post("/api/compute/scan")
def compute_scan(user: UserDep, session: SessionDep) -> dict:
    ids = list(session.scalars(select(DbConnection.id).where(DbConnection.user_id == user.id, DbConnection.kind == "snowflake")))
    session.commit()  # each account is read with nothing held, then recorded in its own short step
    now = utcnow()
    for cid in ids:
        with session_scope() as s:
            conn = s.get(DbConnection, cid)
            if conn is not None:
                C.scan(s, conn, now)
        C.deliver(cid, now)
    session.expire_all()
    return C.report(session, user)


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price: float | None = Field(default=None, ge=0, le=100)            # $ per credit; null: the contract rate or the list price
    storage_tb: float | None = Field(default=None, ge=0, le=1000)      # $ per TB-month
    budget: float | None = Field(default=None, ge=0, le=10_000_000)   # $ a month for compute; null: none
    warehouse_budgets: dict[Annotated[str, Field(max_length=255)], Annotated[float, Field(ge=0, le=10_000_000)]] | None = None
    runaway_minutes: int | None = Field(default=None, ge=5, le=24 * 60)
    spike_factor: float | None = Field(default=None, ge=1.5, le=20)
    pipeline_users: list[Annotated[str, Field(min_length=1, max_length=255)]] | None = Field(default=None, max_length=20)
    reset_pipeline_users: bool = False  # back to the bot's guess
    alerts_on: bool | None = None


@router.put("/api/compute/{conn_id}/settings")
def compute_settings(conn_id: Annotated[int, Path()], body: SettingsIn, user: UserDep, session: SessionDep) -> dict:
    c = _mine(session, user, conn_id)
    st = C.state_of(session, c.id)
    cur = dict(st.get("settings") or {})
    for k, v in body.model_dump(exclude_unset=True).items():
        if k == "reset_pipeline_users":
            if v:
                cur["pipeline_users"] = None
            continue
        if k == "warehouse_budgets" and v is not None:
            v = {w: b for w, b in v.items() if b}
        cur[k] = v
    st["settings"] = cur
    C.save_state(session, c.id, st)
    return C.view(session, c)


class ScriptIn(BaseModel):
    ids: list[Annotated[str, Field(min_length=3, max_length=300)]] = Field(min_length=1, max_length=200)


@router.post("/api/compute/{conn_id}/script")
def compute_script(conn_id: Annotated[int, Path()], body: ScriptIn, user: UserDep, session: SessionDep) -> dict:
    c = _mine(session, user, conn_id)
    return C.script(C.state_of(session, c.id), body.ids, user.name, c.host or c.name)
