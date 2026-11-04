"""Sync what happened in the project into the metadata DB.

1. `sync_runs`: trigger rows joined with their pipeline runs → `runs`.
2. `observe_runs`: for every finished, successful run not yet measured, compute
   its output metrics (rows, null rates, duplicates, duration) →
   `metric_observations`. These feed baselines and the portal charts.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from ..db import MetricObservation, Run
from ..timeutil import local_weekday
from .context import ProjectContext

log = logging.getLogger(__name__)

TERMINAL = {"Succeeded", "Failed", "Cancelled"}


def sync_runs(ctx: ProjectContext, lookback_hours: int) -> list[Run]:
    cfg = ctx.cfg
    since = ctx.now - timedelta(hours=lookback_hours)
    source = ctx.connectors.source(cfg.trigger_table.source)
    trigger_rows = source.fetch_trigger_rows(cfg.trigger_table, since)

    existing = {
        r.trigger_id: r
        for r in ctx.session.scalars(
            select(Run).where(Run.project_id == ctx.project.id, Run.requested_at >= since - timedelta(hours=1))
        )
    }

    runs: list[Run] = []
    to_fetch: list[str] = []
    for t in trigger_rows:
        run = existing.get(t.trigger_id)
        if run is None:
            run = Run(project_id=ctx.project.id, trigger_id=t.trigger_id, site=t.site, requested_at=t.requested_at)
            ctx.session.add(run)
        run.trigger_status = t.state
        run.run_id = t.run_id
        run.site = t.site
        runs.append(run)
        if t.run_id and run.run_status not in TERMINAL:
            to_fetch.append(t.run_id)

    pipeline_runs = ctx.connectors.pipeline.get_runs(to_fetch) if to_fetch else {}
    for run in runs:
        pr = pipeline_runs.get(run.run_id or "")
        if pr is None:
            continue
        run.pipeline = pr.pipeline
        run.run_status = pr.status
        run.started_at = pr.started_at
        run.ended_at = pr.ended_at
        run.duration_s = pr.duration_s
        run.error = pr.message or None
    ctx.session.flush()
    return runs


def observe_runs(ctx: ProjectContext, runs: list[Run]) -> int:
    """Measure output metrics for finished runs. Idempotent: each run is measured once."""
    grace = timedelta(minutes=ctx.settings.load_grace_minutes)
    tz = ctx.settings.tz
    measured = 0
    for run in runs:
        if run.observed or run.run_status != "Succeeded" or not run.started_at or not run.ended_at:
            continue
        if ctx.now < run.ended_at + grace:
            continue  # late rows may still be landing
        ref = run.run_id or run.trigger_id
        weekday = local_weekday(run.requested_at, tz)
        values: list[tuple[str, str, str, float]] = []  # (output_id, metric, field, value)

        total_rows = 0
        for out in ctx.cfg.outputs:
            m = ctx.connectors.source(out.source).window_metrics(out, run.site, run.started_at, run.ended_at + grace)
            total_rows += m.row_count
            values.append((out.id, "row_count", "", m.row_count))
            if m.row_count:
                values += [(out.id, "null_rate", f, rate) for f, rate in m.null_rates.items()]
                values.append((out.id, "duplicate_rate", "", m.duplicate_rate))
        if run.duration_s is not None:
            values.append(("_pipeline", "duration_s", "", run.duration_s))

        ctx.session.add_all(
            MetricObservation(
                project_id=ctx.project.id, output_id=output_id, site=run.site, metric=metric, field=field,
                run_ref=ref, weekday=weekday, value=float(value), observed_at=run.ended_at,
            )
            for output_id, metric, field, value in values
        )
        run.rows_loaded = total_rows
        run.observed = True
        measured += 1
    ctx.session.flush()
    return measured
