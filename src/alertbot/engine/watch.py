"""Live run watching: notice a new or changed pipeline run within seconds, not at the next scheduled round.

Every ~30 seconds per project (60 for Logic Apps, which are listed per workflow) the worker asks Azure one cheap
question: which runs started or changed since I last looked? If any run is new, or its status moved on (queued →
running → succeeded / failed, or a rerun after a fix), the project's full round runs straight away: runs are synced,
measured and checked, incidents open or resolve, and the portal picks it up on its next poll.

What changed is kept (the last few events per project), so the portal can say "DEV-SG-TERMINAL-CSV just succeeded".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..config import ProjectConfig, load_projects
from ..db import BotCycle, BotState, Run, session_scope
from ..timeutil import iso, utcnow

log = logging.getLogger(__name__)

INTERVAL_S = {"logic_apps": 60}  # everything else: DEFAULT_INTERVAL_S
DEFAULT_INTERVAL_S = 30
KEEP_EVENTS = 20


@dataclass
class Change:
    run_id: str
    pipeline: str
    status: str
    before: str | None  # None: a run we hadn't seen
    at: datetime | None


def _key(cfg: ProjectConfig) -> str:
    return f"watch:{cfg.project}"


def _parse(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v)).replace(tzinfo=None) if v else None
    except ValueError:
        return None


def look(cfg: ProjectConfig, now: datetime | None = None, force: bool = False) -> list[Change] | None:
    """Runs that are new or changed since the last look; None when it isn't time to look yet (or nothing to watch)."""
    from .adf import inventory_of
    from .cycle import _connectors, ensure_project

    if cfg.kind != "adf" or not cfg.active or cfg.pipeline is None:
        return None
    now = now or utcnow()
    with session_scope() as s:
        row = s.get(BotState, _key(cfg))
        state = (row.value if row else None) or {}
        last = _parse(state.get("at"))
        if not force and last and (now - last).total_seconds() < INTERVAL_S.get(cfg.pipeline.type, DEFAULT_INTERVAL_S):
            return None
        project = ensure_project(s, cfg)
        if s.scalars(select(BotCycle.id).where(BotCycle.project_id == project.id).limit(1)).first() is None:
            return None  # the first round (with its history backfill) belongs to the scheduler
        known = {r.trigger_id: r.run_status for r in s.scalars(
            select(Run).where(Run.project_id == project.id, Run.requested_at >= now - timedelta(days=3)))}
        watched = {i["name"] for i in inventory_of(s, cfg.project).get("items", []) if i.get("watched", True)}

    # Data Factory / Synapse: runs updated since the last look (with overlap). Logic Apps can only filter on start
    # time, so look back far enough to see long runs finish.
    since = now - timedelta(hours=6) if cfg.pipeline.type == "logic_apps" else (last or now - timedelta(hours=2)) - timedelta(minutes=2)
    conn = _connectors(cfg).pipeline
    remote = conn.query_runs(since, now + timedelta(minutes=5), pipelines=sorted(watched) or None)  # type: ignore[attr-defined]
    changes = [Change(r.run_id, r.pipeline, r.status, known.get(r.run_id), r.ended_at or r.started_at)
               for r in remote if (not watched or r.pipeline in watched) and known.get(r.run_id) != r.status]

    with session_scope() as s:
        row = s.get(BotState, _key(cfg)) or BotState(key=_key(cfg))
        prev = (row.value or {}).get("events", [])
        events = [{"run_id": c.run_id, "pipeline": c.pipeline, "status": c.status, "before": c.before, "at": iso(c.at),
                   "seen_at": iso(now)} for c in changes]
        row.value = {"at": iso(now), "events": (events + prev)[:KEEP_EVENTS],
                     "last_change_at": iso(now) if changes else (row.value or {}).get("last_change_at")}
        row.updated_at = now
        s.add(row)
    return changes


def watch_all(now: datetime | None = None) -> list[tuple[str, list[Change], Any]]:
    """Worker: look at every project that's due a look; run the project's round at once when something changed."""
    from .cycle import run_cycle

    out = []
    for cfg in load_projects().values():
        try:
            changes = look(cfg, now)
        except Exception as exc:  # noqa: BLE001 - one unreachable factory must not stop the others
            log.info("watching %s failed: %s", cfg.project, exc)
            continue
        if changes:
            out.append((cfg.project, changes, run_cycle(cfg, now=now)))
    return out


def state_of(session: Any, project_key: str) -> dict[str, Any]:
    row = session.get(BotState, f"watch:{project_key}")
    return (row.value if row else None) or {}
