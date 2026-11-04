"""Seed projects (from config) and people (from config/members.yaml)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import load_member_seed, load_projects
from .db import Project, User, UserProject
from .engine.cycle import ensure_project


def seed(session: Session) -> tuple[int, int]:
    projects = {cfg.project: ensure_project(session, cfg) for cfg in load_projects().values()}
    users = 0
    for m in load_member_seed():
        user = session.scalars(select(User).where(User.email == m.email.lower())).first()
        if user is None:
            user = User(email=m.email.lower(), name=m.name, is_admin=m.admin)
            session.add(user)
            session.flush()
        user.name, user.is_admin = m.name, m.admin
        users += 1
        for key, role in m.projects.items():
            project = projects.get(key)
            if project is None:
                continue
            set_membership(session, user, project, role)
    return len(projects), users


def set_membership(session: Session, user: User, project: Project, role: str) -> None:
    link = session.get(UserProject, (user.id, project.id))
    if link is None:
        session.add(UserProject(user_id=user.id, project_id=project.id, role=role))
    else:
        link.role = role
