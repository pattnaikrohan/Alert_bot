"""One bot cycle per project:

    sync trigger rows + pipeline runs
      â†’ measure finished runs (metric observations)
        â†’ run checks against learned baselines
          â†’ group failures into incidents (+ investigate)
            â†’ dispatch alerts from the outbox

Each project runs in its own transaction; one project failing never stops the
others, and the failure itself is recorded as the cycle's heartbeat.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import ProjectConfig, load_projects
from ..connectors import ProjectConnectors, build_connectors
from ..db import BotCycle, CheckResult, Project, session_scope
from ..settings import get_settings
from ..timeutil import utcnow
from .adf import AdfEvaluator, observe_adf_runs, sync_adf_runs
from .checks import Evaluator, Outcome
from .context import ProjectContext
from .feeds import FeedEvaluator, observe_feed_runs, sync_feed_runs
from .incidents import IncidentChanges, process
from .notify import dispatch
from .observe import observe_runs, sync_runs

log = logging.getLogger(__name__)

STALE_LOCK = timedelta(minutes=15)
FIRST_BACKFILL_DAYS = 7


@dataclass
class CycleReport:
    project: str
    status: str
    runs: int = 0
    measured: int = 0
    checks: int = 0
    failed_checks: int = 0
    changes: IncidentChanges = field(default_factory=IncidentChanges)
    notifications: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    error: str | None = None


_CONNECTORS: dict[str, tuple[ProjectConfig, ProjectConnectors]] = {}


def _connectors(cfg: ProjectConfig) -> ProjectConnectors:
    cached = _CONNECTORS.get(cfg.project)
    if cached is None or cached[0] != cfg:
        cached = _CONNECTORS[cfg.project] = (cfg, build_connectors(cfg))
    return cached[1]


def ensure_project(session: Session, cfg: ProjectConfig) -> Project:
    project = session.scalars(select(Project).where(Project.key == cfg.project)).first()
    if project is None:
        project = Project(key=cfg.project, display_name=cfg.display_name, description=cfg.description, active=cfg.active)
        session.add(project)
        session.flush()
    else:
        project.display_name, project.description, project.active = cfg.display_name, cfg.description, cfg.active
    return project


def sync_projects() -> None:
    with session_scope() as s:
        for cfg in load_projects().values():
            ensure_project(s, cfg)


def make_context(session: Session, cfg: ProjectConfig, now: datetime | None = None) -> ProjectContext:
    return ProjectContext(cfg=cfg, project=ensure_project(session, cfg), session=session,
                          connectors=_connectors(cfg), settings=get_settings(), now=now or utcnow())


def _store(ctx: ProjectContext, cycle: BotCycle, outcomes: list[Outcome]) -> dict[int, CheckResult]:
    rows: dict[int, CheckResult] = {}
    for o in outcomes:
        row = CheckResult(
            cycle_id=cycle.id, project_id=ctx.project.id, check=o.check, scope=o.scope, status=o.status,
            severity=o.severity if o.failed else None, message=o.message, actual=o.actual,
            expected_low=o.expected_low, expected_high=o.expected_high, run_ref=o.run_ref,
            details=o.details, created_at=ctx.now,
        )
        ctx.session.add(row)
        rows[id(o)] = row
    ctx.session.flush()
    return rows


def _collect_quality(cfg: ProjectConfig, now: datetime, force: bool = False) -> object | None:
    """Read the warehouse before the cycle's transaction: those queries can take a while and must not hold the database."""
    if cfg.kind != "adf":
        return None
    from ..dq.service import collect_mapped
    from .quality import collect_quality

    try:
        with session_scope() as s:
            ctx = make_context(s, cfg, now)
            mapped = collect_mapped(ctx, force)  # tables people mapped on databases they connected
            if mapped is not None:
                return mapped
            if cfg.quality is None or not cfg.quality.enabled:
                return None
            return collect_quality(ctx, force)
    except Exception:  # noqa: BLE001 - data checks must never stop the pipeline checks
        log.exception("data-quality collection failed for %s", cfg.project)
        return None


def _explain_quality(cfg: ProjectConfig, quality: object | None) -> None:
    """The AI (or, without one, the rules) explains each mapped table that was just checked: automatic, after every load."""
    ids = getattr(quality, "mapped_ids", None)
    if not ids or not getattr(quality, "items", None):
        return
    from ..dq.graph import run_dq
    from ..dq.service import set_live

    try:
        with session_scope() as s:
            run_dq(make_context(s, cfg, utcnow()), ids, stage="analyze")
    except Exception:  # noqa: BLE001 - the explanation is a bonus; the checks and incidents are already saved
        log.exception("data-quality explanation failed for %s", cfg.project)
    finally:
        set_live(cfg.project, state="idle")


def run_cycle(cfg: ProjectConfig, *, now: datetime | None = None, alert: bool = True, force_quality: bool = False) -> CycleReport:
    from ..ai.usage import tagged

    with tagged(project=cfg.project):  # every AI call this round makes is metered against the project
        return _run_cycle(cfg, now=now, alert=alert, force_quality=force_quality)


def _run_cycle(cfg: ProjectConfig, *, now: datetime | None = None, alert: bool = True, force_quality: bool = False) -> CycleReport:
    started = time.perf_counter()
    report = CycleReport(project=cfg.project, status="ok")
    now = now or utcnow()
    quality = _collect_quality(cfg, now, force_quality)
    try:
        with session_scope() as s:
            ctx = make_context(s, cfg, now)
            ctx.quality = quality
            running = s.scalars(
                select(BotCycle).where(BotCycle.project_id == ctx.project.id, BotCycle.status == "running",
                                       BotCycle.started_at > now - STALE_LOCK)
            ).first()
            if running is not None:
                report.status = "skipped"
                report.error = f"cycle {running.id} still running since {running.started_at:%H:%M:%S}"
                if getattr(quality, "mapped_ids", None):
                    from ..dq.service import set_live

                    set_live(cfg.project, state="idle")
                return report
            cycle = BotCycle(project_id=ctx.project.id, started_at=now, status="running")
            s.add(cycle)
            s.flush()

            if cfg.kind == "feeds":
                runs = sync_feed_runs(ctx, ctx.settings.lookback_hours)
                report.runs = len(runs)
                report.measured = observe_feed_runs(ctx, runs)
                outcomes = FeedEvaluator(ctx, runs).run()
            elif cfg.kind == "adf":
                runs = sync_adf_runs(ctx, ctx.settings.lookback_hours)
                report.runs = len(runs)
                report.measured = observe_adf_runs(ctx, runs)
                outcomes = AdfEvaluator(ctx, runs).run()
            else:
                runs = sync_runs(ctx, ctx.settings.lookback_hours)
                report.runs = len(runs)
                report.measured = observe_runs(ctx, runs)
                outcomes = Evaluator(ctx, runs).run()
            results = _store(ctx, cycle, outcomes)
            report.checks = sum(o.status in ("pass", "fail") for o in outcomes)
            report.failed_checks = sum(o.failed for o in outcomes)
            report.changes = process(ctx, outcomes, results, alert=alert)
            ai_queue = list(dict.fromkeys(ctx.ai_queue))

            cycle.status, cycle.finished_at = "ok", utcnow()
            cycle.checks_run, cycle.checks_failed = report.checks, report.failed_checks
        # committed: now the slow part, outside the transaction, then send alerts (with the AI text when it's ready)
        if ai_queue:
            from ..ai.investigator import enrich_with_ai

            for incident_id in ai_queue:
                try:
                    enrich_with_ai(cfg, incident_id)
                except Exception:  # noqa: BLE001 - AI must never break the cycle
                    log.exception("AI enrichment failed for incident %s", incident_id)
        with session_scope() as s:
            report.notifications = dispatch(make_context(s, cfg, now))
    except Exception as exc:
        log.exception("cycle failed for %s", cfg.project)
        report.status, report.error = "error", f"{type(exc).__name__}: {exc}"
        with session_scope() as s:
            project = ensure_project(s, cfg)
            s.add(BotCycle(project_id=project.id, started_at=now, finished_at=utcnow(), status="error", error=report.error[:4000]))
    _explain_quality(cfg, quality)
    report.seconds = round(time.perf_counter() - started, 2)
    return report


def backfill(cfg: ProjectConfig, days: int = 60, now: datetime | None = None) -> int:
    """Measure historical runs so baselines exist from day one. Never alerts."""
    with session_scope() as s:
        ctx = make_context(s, cfg, now)
        if cfg.kind == "feeds":
            return observe_feed_runs(ctx, sync_feed_runs(ctx, lookback_hours=days * 24))
        if cfg.kind == "adf":
            return observe_adf_runs(ctx, sync_adf_runs(ctx, lookback_hours=days * 24))
        runs = sync_runs(ctx, lookback_hours=days * 24)
        return observe_runs(ctx, runs)


def is_due(cfg: ProjectConfig, last_started: datetime | None, now: datetime) -> bool:
    if last_started is None:
        return True
    return croniter(cfg.schedule, last_started).get_next(datetime) <= now


def run_due(force: bool = False) -> list[CycleReport]:
    now = utcnow()
    reports = []
    for cfg in load_projects().values():
        if not cfg.active:
            continue
        if not force:
            with session_scope() as s:
                project = ensure_project(s, cfg)
                last = s.scalars(
                    select(BotCycle.started_at).where(BotCycle.project_id == project.id)
                    .order_by(BotCycle.started_at.desc()).limit(1)
                ).first()
            if not is_due(cfg, last, now):
                continue
            if last is None and cfg.kind == "adf":
                # a project the bot hasn't watched before (e.g. just found in Azure): learn a week of history first,
                # so durations and volumes have a normal to compare against from the first round
                try:
                    backfill(cfg, days=FIRST_BACKFILL_DAYS, now=now)
                except Exception:  # noqa: BLE001 - the cycle below reports the real problem (e.g. no access)
                    log.warning("first backfill for %s failed", cfg.project, exc_info=True)
        reports.append(run_cycle(cfg, now=now))
    return reports
