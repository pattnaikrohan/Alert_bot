"""The AI usage page: every model call the bot made, in real tokens, by feature, model, day and project; what it
actually cost; and what the same work would have cost on other providers' models at their live prices."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from ..ai import usage
from ..db import BotState, Project, UserProject
from .auth import SessionDep, UserDep

router = APIRouter()


def _visible(session: SessionDep, user: UserDep) -> list[str] | None:
    """Admins see every call; everyone else sees their projects' calls, their own, and calls for no project."""
    if user.is_admin:
        return None
    return list(session.scalars(select(Project.key).join(UserProject, UserProject.project_id == Project.id)
                                .where(UserProject.user_id == user.id)))


@router.get("/api/ai/usage")
def ai_usage(user: UserDep, session: SessionDep, days: Annotated[int, Query(ge=1, le=365)] = 30,
             models: Annotated[str | None, Query(max_length=2000)] = None) -> dict:
    usage.flush()  # include the calls made a moment ago
    keys = _visible(session, user)
    chosen = [m for m in (models or "").split(",") if m] or None
    if chosen is None:
        prefs = session.get(BotState, f"prefs:user:{user.id}")
        chosen = ((prefs.value or {}).get("ai_scenarios") if prefs else None) or None
    session.commit()  # nothing held while the price list may be fetched
    return usage.summary(session, days=days, project_keys=keys, user_id=user.id, scenarios=chosen)


@router.get("/api/ai/models")
def ai_models(user: UserDep, session: SessionDep, q: Annotated[str, Query(max_length=80)] = "") -> dict:
    """Models anyone can price the work on: OpenRouter's live list, newest first."""
    session.commit()
    return {"models": usage.price_list(session, q)}
