from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import ProjectConfig
from ..connectors import ProjectConnectors
from ..db import Project
from ..settings import Settings


@dataclass
class ProjectContext:
    """Everything one project's cycle needs, passed explicitly instead of via globals."""

    cfg: ProjectConfig
    project: Project
    session: Session
    connectors: ProjectConnectors
    settings: Settings
    now: datetime
    # incidents to enrich with AI after the cycle's transaction commits (AI never runs while holding the database)
    ai_queue: list[int] = field(default_factory=list)
    # warehouse profiles read before the cycle's transaction (slow queries never hold the database)
    quality: Any = None

    @property
    def key(self) -> str:
        return self.cfg.project
