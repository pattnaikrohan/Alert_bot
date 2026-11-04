"""Snowflake Cortex AI usage: what Cortex costs on the accounts you connected, by product, user, model and agent.

    GET  /api/cortex          every Snowflake account I connected: the Cortex lines of the bill, and where they went
    POST /api/cortex/scan     read my accounts now
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from ..db import DbConnection, session_scope
from ..dq import cortex as X
from ..timeutil import utcnow
from .auth import SessionDep, UserDep

router = APIRouter()


@router.get("/api/cortex")
def cortex_report(user: UserDep, session: SessionDep) -> dict:
    return X.report(session, user.id)


@router.post("/api/cortex/scan")
def cortex_scan(user: UserDep, session: SessionDep) -> dict:
    ids = list(session.scalars(select(DbConnection.id).where(DbConnection.user_id == user.id, DbConnection.kind == "snowflake")))
    session.commit()  # each account is read with nothing held, then recorded in its own short step
    now = utcnow()
    for cid in ids:
        with session_scope() as s:
            conn = s.get(DbConnection, cid)
            if conn is not None:
                X.scan(s, conn, now)
    session.expire_all()
    return X.report(session, user.id)
