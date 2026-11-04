"""Charts people ask the bot for, pinned to a project's overview. Each person sees only their own."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

from ..db import UserChart
from ..engine.charts import ChartError, ChartSpec, compute, known_names
from ..timeutil import iso, utcnow
from .auth import AccessDep, SessionDep

router = APIRouter()
MAX_CHARTS = 24


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1200)


class Ask(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=20)


class Pin(BaseModel):
    spec: dict[str, Any]


def _data(session: SessionDep, access: AccessDep, spec: ChartSpec) -> dict[str, Any]:
    try:
        return compute(session, access.project, spec, utcnow())
    except ChartError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/projects/{key}/charts/ask")
def ask_for_chart(body: Ask, access: AccessDep, session: SessionDep) -> dict:
    """The AI reads the request and proposes a chart (or asks back); the bot computes it. Nothing is pinned yet."""
    from ..ai.charts import understand
    from ..ai.llm import AIUnavailable
    from ..ai.usage import tagged

    try:
        with tagged(project=access.project.key, user_id=access.user.id, feature="charts"):
            reply, spec, model = understand([m.model_dump() for m in body.messages], known_names(session, access.project))
    except AIUnavailable as exc:
        raise HTTPException(503, f"The AI isn't available right now ({exc}). Your pinned charts keep working.") from exc
    if spec is None:
        return {"reply": reply, "spec": None, "data": None, "model": model}
    try:
        data = compute(session, access.project, spec, utcnow())
    except ChartError as exc:
        return {"reply": str(exc), "spec": None, "data": None, "model": model}
    return {"reply": reply, "spec": spec.model_dump(), "data": data, "model": model}


@router.get("/api/projects/{key}/charts")
def my_charts(access: AccessDep, session: SessionDep) -> dict:
    """My pinned charts for this project, with fresh numbers."""
    rows = session.scalars(select(UserChart).where(UserChart.user_id == access.user.id, UserChart.project_key == access.project.key)
                           .order_by(UserChart.position, UserChart.id))
    out = []
    for c in rows:
        try:
            spec = ChartSpec.model_validate(c.spec)
            data, error = compute(session, access.project, spec, utcnow()), None
        except (ValidationError, ChartError) as exc:
            spec, data, error = None, None, str(exc).splitlines()[0][:200]
        out.append({"id": c.id, "spec": spec.model_dump() if spec else c.spec, "data": data, "error": error, "created_at": iso(c.created_at)})
    return {"charts": out}


@router.post("/api/projects/{key}/charts")
def pin_chart(body: Pin, access: AccessDep, session: SessionDep) -> dict:
    try:
        spec = ChartSpec.model_validate(body.spec)
    except ValidationError as exc:
        raise HTTPException(422, f"That chart isn't valid: {exc.errors()[0]['msg']}") from exc
    data = _data(session, access, spec)
    count = session.scalar(select(func.count(UserChart.id)).where(UserChart.user_id == access.user.id,
                                                                 UserChart.project_key == access.project.key)) or 0
    if count >= MAX_CHARTS:
        raise HTTPException(409, f"You can pin up to {MAX_CHARTS} charts here; remove one first.")
    c = UserChart(user_id=access.user.id, project_key=access.project.key, spec=spec.model_dump(), position=count)
    session.add(c)
    session.flush()
    return {"chart": {"id": c.id, "spec": c.spec, "data": data, "error": None, "created_at": iso(c.created_at)}}


@router.delete("/api/projects/{key}/charts/{chart_id}")
def unpin_chart(chart_id: Annotated[int, Path()], access: AccessDep, session: SessionDep) -> dict:
    c = session.get(UserChart, chart_id)
    if c is None or c.user_id != access.user.id or c.project_key != access.project.key:
        raise HTTPException(404, "chart not found")  # someone else's chart looks exactly like a missing one
    session.delete(c)
    session.flush()
    return {"removed": chart_id}
