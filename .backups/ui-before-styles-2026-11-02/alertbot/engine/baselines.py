"""Learned baselines.

For each (project, output, site, metric, field), "normal" is learned from that
site's own history, so nobody has to set thresholds by hand:

1. same weekday, the last 8 such runs in the last 8 weeks (needs at least 4);
2. otherwise, every day in the last 4 weeks (needs at least 5);
3. otherwise no baseline yet: learned checks skip, pinned rules still apply.

Spread uses the median absolute deviation, so one past bad run does not
widen the band the way a standard deviation would.

It keeps learning, every run:
· a new level becomes normal by itself: when the latest runs are all outside the band on the same side, the band is
  learned from those runs alone. A change that isn't bad news (more rows, longer runs) is adopted after 3 runs over
  at least 18 hours; bad news (fewer rows, more empty values, duplicates…) only after a week of it, and a drop to zero
  rows never is;
· someone can say "this is the new normal" on an incident (a BaselineReset): from then on only newer runs count.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from ..db import Baseline, BaselineReset, MetricObservation
from .context import ProjectContext

WEEKDAY_MIN_SAMPLES = 4
WEEKDAY_MAX_SAMPLES = 8
ALL_DAYS_MIN_SAMPLES = 5
K = 4.0  # band half-width in robust standard deviations
SHIFT_RUNS = 3                        # a level shift needs this many runs in a row on the same side of the band …
SHIFT_SPAN = timedelta(hours=18)      # … spread over at least this long (bad news: BAD_SHIFT_SPAN)
BAD_SHIFT_SPAN = timedelta(days=7)
BAD_WHEN_UP = {"null_rate", "empty_rate", "duplicate_rate", "invalid_rate", "out_of_range_rate", "negative_rate", "orphan_rate",
               "rule_fail_rate", "freshness_hours", "rows_skipped"}
BAD_WHEN_DOWN = {"row_count", "rows_read", "rows_total", "days_recent"}
MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class Band:
    metric: str
    median: float
    low: float
    high: float
    samples: int
    basis: str  # weekday | all_days | new_level (adapted by itself) | accepted (someone accepted a new normal)
    weekday: int  # -1 when basis is all_days


def robust_band(values: list[float], metric: str, *, min_factor: float = 1.5) -> tuple[float, float, float]:
    med = statistics.median(values)
    sigma = MAD_TO_SIGMA * statistics.median(abs(v - med) for v in values)
    if metric == "row_count":
        spread = max(K * sigma, 0.15 * med, 5.0)
        return med, max(0.0, med - spread), med + spread
    if metric in ("null_rate", "duplicate_rate"):
        high = max(med + K * sigma, med * 3, med + 0.02)
        return med, 0.0, min(1.0, high)
    if metric == "duration_s":
        spread = max(K * sigma, med * (min_factor - 1))
        return med, max(0.0, med - spread), med + spread
    spread = max(K * sigma, 0.1 * abs(med))
    return med, med - spread, med + spread


def band_for(
    ctx: ProjectContext,
    output_id: str,
    site: str,
    metric: str,
    field: str,
    weekday: int,
    *,
    before: datetime,
    exclude_run: str | None = None,
    min_factor: float = 1.5,
    persist: bool = False,
) -> Band | None:
    reset = latest_reset(ctx, output_id, site, metric, field, before)
    base = select(MetricObservation.value, MetricObservation.weekday, MetricObservation.observed_at).where(
        MetricObservation.project_id == ctx.project.id,
        MetricObservation.output_id == output_id,
        MetricObservation.site == site,
        MetricObservation.metric == metric,
        MetricObservation.field == field,
        MetricObservation.observed_at < before,
        MetricObservation.observed_at >= before - timedelta(days=WEEKDAY_MAX_SAMPLES * 7),
    )
    if exclude_run:
        base = base.where(MetricObservation.run_ref != exclude_run)
    if reset is not None:
        base = base.where(MetricObservation.observed_at >= reset.since)
    rows = ctx.session.execute(base.order_by(MetricObservation.observed_at.desc())).all()

    same_day = [v for v, wd, _ in rows if wd == weekday][:WEEKDAY_MAX_SAMPLES]
    recent = [v for v, _, _ in rows][: 28 * 3]

    band: Band | None = None
    if reset is not None and rows:
        # accepted as the new normal: learn from what came after, generously while there's little of it
        vals = recent[:28]
        med, low, high = robust_band(vals, metric, min_factor=min_factor)
        if len(vals) < ALL_DAYS_MIN_SAMPLES:
            spread = max(high - med, 0.6 * abs(med), 5.0 if metric == "row_count" else 0.0)
            low, high = (max(0.0, med - spread) if metric != "mean" else med - spread), med + spread
        band = Band(metric, med, low, high, len(vals), "accepted", -1)
    elif len(same_day) >= WEEKDAY_MIN_SAMPLES:
        med, low, high = robust_band(same_day, metric, min_factor=min_factor)
        band = Band(metric, med, low, high, len(same_day), "weekday", weekday)
    elif len(recent) >= ALL_DAYS_MIN_SAMPLES:
        med, low, high = robust_band(recent, metric, min_factor=min_factor)
        band = Band(metric, med, low, high, len(recent), "all_days", -1)
    if band is not None and band.basis != "accepted":
        band = adapt_to_new_level(band, [(v, at) for v, _, at in rows], metric, min_factor) or band

    if band is not None and persist:
        _persist(ctx, output_id, site, metric, field, band)
    return band


def _persist(ctx: ProjectContext, output_id: str, site: str, metric: str, field: str, band: Band) -> None:
    row = ctx.session.scalars(
        select(Baseline).where(
            Baseline.project_id == ctx.project.id,
            Baseline.output_id == output_id,
            Baseline.site == site,
            Baseline.metric == metric,
            Baseline.field == field,
            Baseline.weekday == band.weekday,
        )
    ).first()
    if row is None:
        row = Baseline(project_id=ctx.project.id, output_id=output_id, site=site, metric=metric, field=field, weekday=band.weekday)
        ctx.session.add(row)
    row.basis, row.samples, row.median, row.low, row.high = band.basis, band.samples, band.median, band.low, band.high
    row.updated_at = ctx.now


def adapt_to_new_level(band: Band, newest_first: list[tuple[float, datetime]], metric: str, min_factor: float = 1.5) -> Band | None:
    """If the latest runs all sit outside the band on the same side for long enough, they are the new normal."""
    streak: list[tuple[float, datetime]] = []
    side = 0
    for v, at in newest_first:
        s = 1 if v > band.high else -1 if v < band.low else 0
        if s == 0 or (side and s != side):
            break
        side = s
        streak.append((v, at))
    if len(streak) < SHIFT_RUNS:
        return None
    bad = (side > 0 and metric in BAD_WHEN_UP) or (side < 0 and metric in BAD_WHEN_DOWN)
    span = streak[0][1] - streak[-1][1]
    if span < (BAD_SHIFT_SPAN if bad else SHIFT_SPAN):
        return None
    values = [v for v, _ in streak][:28]
    if metric in BAD_WHEN_DOWN and statistics.median(values) == 0:
        return None  # nothing arriving is never "normal"
    med, low, high = robust_band(values, metric, min_factor=min_factor)
    return Band(metric, med, low, high, len(values), "new_level", -1)


def latest_reset(ctx: ProjectContext, output_id: str, site: str, metric: str, field: str, before: datetime) -> BaselineReset | None:
    """The newest "this is the new normal" that covers this measurement (empty parts match everything)."""
    from sqlalchemy import or_

    return ctx.session.scalars(select(BaselineReset).where(
        BaselineReset.project_id == ctx.project.id, BaselineReset.metric == metric, BaselineReset.since <= before,
        or_(BaselineReset.output_id.is_(None), BaselineReset.output_id == output_id),
        or_(BaselineReset.site.is_(None), BaselineReset.site == site),
        or_(BaselineReset.field.is_(None), BaselineReset.field == field)).order_by(BaselineReset.since.desc()).limit(1)).first()
