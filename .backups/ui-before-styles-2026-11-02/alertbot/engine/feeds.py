"""Feeds projects: every scheduled run of every feed becomes a `Run`, so the rest of the bot (incidents, alerts,
learned baselines, portal) works unchanged.

For each feed and each expected slot (cron in the project timezone):
    rows landed in [slot - skew, next slot)  → Succeeded, with first/last row time and the count
    none yet and still within the grace time → InProgress
    none after the grace time                → Failed (a missed run)   … or NoData for event-driven feeds
Volume, empty fields and duplicates are measured per run once its grace time has passed.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select

from ..config import FEED_OUTPUT_ID, FeedConfig
from ..connectors.feeds import FeedProducers, FeedSource, ProducerUnavailable
from ..db import MetricObservation, Run, SchemaSnapshot
from ..timeutil import WEEKDAYS, iso, local_weekday
from .checks import Evaluator, Outcome, diff_schema, schema_fingerprint
from .context import ProjectContext

SKEW = timedelta(minutes=5)  # producers sometimes write a little before the nominal slot
EVENT_DRIVEN_CHECKS = ("missed_run", "row_count")


def feed_checks(feed: FeedConfig) -> set[str]:
    """Checks that apply to a feed. Event-driven feeds (rows only when something happens, e.g. cyclone positions)
    have no expectation of rows per run, so missed-run and volume checks are off unless explicitly enabled."""
    base = {"missed_run", "row_count", "null_spike", "duplicates", "freshness", "schema_drift", "producer_failed"}
    if feed.producer.type == "manual" or feed.event_driven:
        base -= set(EVENT_DRIVEN_CHECKS)
    return base


def project_tz(ctx: ProjectContext) -> ZoneInfo:
    return ZoneInfo(ctx.cfg.timezone) if ctx.cfg.timezone else ctx.settings.tz


def expected_slots(feed: FeedConfig, tz: ZoneInfo, since: datetime, until: datetime) -> list[datetime]:
    """Scheduled run times in (since, until], as naive UTC."""
    start = since.replace(tzinfo=UTC).astimezone(tz)
    it = croniter(feed.schedule, start)
    out = []
    for _ in range(5000):
        nxt = it.get_next(datetime)
        utc = nxt.astimezone(UTC).replace(tzinfo=None)
        if utc > until:
            break
        out.append(utc)
    return out


def slot_interval(feed: FeedConfig, tz: ZoneInfo, now: datetime) -> timedelta:
    slots = expected_slots(feed, tz, now - timedelta(days=15), now)
    gaps = [(b - a) for a, b in zip(slots, slots[1:], strict=False)]
    return statistics.median(gaps) if gaps else timedelta(days=1)


def _source(ctx: ProjectContext, feed: FeedConfig) -> FeedSource:
    return ctx.connectors.source(feed.source)  # type: ignore[return-value]


def sync_feed_runs(ctx: ProjectContext, lookback_hours: int) -> list[Run]:
    tz = project_tz(ctx)
    now = ctx.now
    since = now - timedelta(hours=lookback_hours)
    existing = {r.trigger_id: r for r in ctx.session.scalars(
        select(Run).where(Run.project_id == ctx.project.id, Run.requested_at >= since - timedelta(days=8)))}
    runs: list[Run] = []
    for feed in ctx.cfg.feeds:
        slots = expected_slots(feed, tz, since, now)
        if not slots:
            continue
        counts = _source(ctx, feed).minute_counts(feed, slots[0] - SKEW, now + timedelta(minutes=1))
        expects_rows = "missed_run" in feed_checks(feed)
        grace = timedelta(minutes=feed.grace_minutes)
        for i, slot in enumerate(slots):
            nxt = slots[i + 1] if i + 1 < len(slots) else None
            lo, hi = slot - SKEW, (nxt - SKEW) if nxt else now + timedelta(minutes=1)
            inside = [(m, n) for m, n in counts if lo <= m < hi]
            n = sum(c for _, c in inside)
            tid = f"{feed.id}@{slot.isoformat()}"
            run = existing.get(tid)
            if run is None:
                run = Run(project_id=ctx.project.id, trigger_id=tid, site=feed.name, requested_at=slot)
                ctx.session.add(run)
            run.run_id = tid
            run.pipeline = feed.producer.name or feed.producer.type
            if n:
                run.trigger_status, run.run_status = "succeeded", "Succeeded"
                run.started_at, run.ended_at = inside[0][0], inside[-1][0] + timedelta(minutes=1)
                run.duration_s = (run.ended_at - slot).total_seconds()
                run.error = None
            elif now < slot + grace:
                run.trigger_status, run.run_status, run.started_at, run.ended_at, run.duration_s = "running", "InProgress", None, None, None
            elif expects_rows:
                run.trigger_status, run.run_status, run.started_at, run.ended_at, run.duration_s = "failed", "Failed", None, None, None
                run.error = (f"No rows reached {feed.table} within {feed.grace_minutes} min of the "
                             f"{slot.replace(tzinfo=UTC).astimezone(tz):%H:%M} run ({feed.producer.name or feed.producer.type}).")
            else:
                run.trigger_status, run.run_status, run.started_at, run.ended_at, run.duration_s = "succeeded", "NoData", None, None, None
                run.error = None
            if run.observed and run.rows_loaded != n and n:
                run.observed = False  # late rows arrived: measure again
            run.rows_loaded = n
            runs.append(run)
    ctx.session.flush()
    return runs


def observe_feed_runs(ctx: ProjectContext, runs: list[Run]) -> int:
    """Measure each successful run once its grace time has passed (later rows are re-measured)."""
    tz = project_tz(ctx)
    by_name = {f.name: f for f in ctx.cfg.feeds}
    measured = 0
    for run in runs:
        feed = by_name.get(run.site)
        if feed is None or run.run_status != "Succeeded" or run.observed:
            continue
        if ctx.now < run.requested_at + timedelta(minutes=feed.grace_minutes):
            continue
        nxt = next((s for s in expected_slots(feed, tz, run.requested_at, run.requested_at + timedelta(days=8))), None)
        end = min((nxt - SKEW) if nxt else ctx.now, ctx.now + timedelta(minutes=1))
        m = _source(ctx, feed).window_metrics(feed, run.requested_at - SKEW, end)
        ref = run.trigger_id
        weekday = local_weekday(run.requested_at, tz)
        ctx.session.query(MetricObservation).filter(MetricObservation.project_id == ctx.project.id, MetricObservation.run_ref == ref).delete()
        values: list[tuple[str, str, float]] = [("row_count", "", m.row_count)]
        if m.row_count:
            values += [("null_rate", f, r) for f, r in m.null_rates.items()]
            if feed.key:
                values.append(("duplicate_rate", "", m.duplicate_rate))
        ctx.session.add_all(
            MetricObservation(project_id=ctx.project.id, output_id=FEED_OUTPUT_ID, site=feed.name, metric=metric, field=field,
                              run_ref=ref, weekday=weekday, value=float(v), observed_at=run.requested_at)
            for metric, field, v in values
        )
        run.rows_loaded = m.row_count
        run.observed = True
        measured += 1
    ctx.session.flush()
    return measured


class FeedEvaluator(Evaluator):
    """The scrape evaluator's volume / null / duplicate logic, applied per feed, plus feed-specific checks."""

    def __init__(self, ctx: ProjectContext, runs: list[Run]):
        super().__init__(ctx, runs)
        self.tz = project_tz(ctx)
        self.by_name = {f.name: f for f in ctx.cfg.feeds}
        self.sites = sorted(set(self.sites) | set(self.by_name))
        self.producers: FeedProducers = ctx.connectors.pipeline  # type: ignore[assignment]

    def outputs_for(self, site: str) -> list[Any]:
        f = self.by_name.get(site)
        if f is None:
            return []
        return [SimpleNamespace(id=FEED_OUTPUT_ID, key=f.key, table=f.table, required_fields=f.required_fields, group_by=None)]

    def run(self) -> list[Outcome]:
        out: list[Outcome] = []
        for site in self.sites:
            feed = self.by_name.get(site)
            if feed is None:
                continue
            checks = feed_checks(feed)
            runs = self.by_site.get(site, [])
            if "missed_run" in checks and self.enabled("missed_run"):
                out.extend(self.missed_run(feed, runs))
            if "freshness" in checks and self.enabled("freshness"):
                out.extend(self.feed_freshness(feed))
            if "producer_failed" in checks and self.enabled("producer_failed"):
                out.extend(self.producer_failed(feed))
            for o in self.output_checks(site, runs):
                if o.check in checks or o.check == "empty_site":
                    out.append(o)
        if self.enabled("schema_drift"):
            out.extend(self.feed_schema_drift())
        return out

    def missed_run(self, feed: FeedConfig, runs: list[Run]) -> list[Outcome]:
        scope = f"site:{feed.name}"
        closed = [r for r in runs if r.run_status in ("Succeeded", "Failed")]
        if not closed:
            return [Outcome("missed_run", scope, "skip", site=feed.name)]
        latest = closed[0]
        if latest.run_status != "Failed":
            return [Outcome("missed_run", scope, "pass", site=feed.name, run_ref=latest.trigger_id)]
        streak = 0
        for r in closed:
            if r.run_status != "Failed":
                break
            streak += 1
        interval = slot_interval(feed, self.tz, self.now)
        sev = "critical" if streak >= 2 or interval >= timedelta(hours=12) else "warning"
        when = latest.requested_at.replace(tzinfo=UTC).astimezone(self.tz)
        msg = (f"{feed.name} wrote nothing for its {when:%H:%M} run" + (f" or the {streak - 1} before it" if streak > 1 else "")
               + f" (expected rows in {feed.table} within {feed.grace_minutes} min).")
        return [Outcome("missed_run", scope, "fail", self._sev("missed_run", sev), msg, actual=0, run_ref=latest.trigger_id, site=feed.name,
                        details={"run_id": latest.trigger_id, "missed_in_a_row": streak, "table": feed.table,
                                 "producer": feed.producer.name or feed.producer.type, "producer_type": feed.producer.type})]

    def feed_freshness(self, feed: FeedConfig) -> list[Outcome]:
        scope = f"site:{feed.name}"
        interval = slot_interval(feed, self.tz, self.now)
        limit = (timedelta(hours=feed.stale_after_hours) if feed.stale_after_hours
                 else interval * 2 + timedelta(minutes=feed.grace_minutes))
        last = _source(self.ctx, feed).last_loaded(feed, self.now - max(limit * 4, timedelta(days=30)))
        if last and self.now - last <= limit:
            return [Outcome("freshness", scope, "pass", site=feed.name, actual=(self.now - last).total_seconds() / 3600,
                            expected_high=limit.total_seconds() / 3600)]
        age = (self.now - last) if last else None
        sev = "critical" if age is None or age > limit * 2 else "warning"
        when = f"since {last.replace(tzinfo=UTC).astimezone(self.tz):%a %d %b %H:%M} ({age.total_seconds() / 3600:.1f} h ago)" if last else "in the last 30 days"
        return [Outcome("freshness", scope, "fail", self._sev("freshness", sev),
                        f"No new rows for {feed.name} {when}; "
                        + (f"the limit is {limit.total_seconds() / 3600:.0f} h." if feed.stale_after_hours
                           else f"it normally writes every {interval.total_seconds() / 3600:.0f} h."),
                        actual=age.total_seconds() / 3600 if age else None, expected_high=limit.total_seconds() / 3600, site=feed.name,
                        details={"last_loaded_at": iso(last), "table": feed.table})]

    def producer_failed(self, feed: FeedConfig) -> list[Outcome]:
        scope = f"site:{feed.name}"
        if feed.producer.type == "manual" or not (feed.producer.app_insights_app_id or feed.producer.type.startswith("snowflake")):
            return [Outcome("producer_failed", scope, "skip", site=feed.name, message="No producer telemetry configured.")]
        try:
            events = self.producers.events(feed, self.now - timedelta(hours=24))
        except ProducerUnavailable as exc:
            return [Outcome("producer_failed", scope, "skip", site=feed.name, message=f"Producer telemetry unavailable: {exc}")]
        if not events:
            return [Outcome("producer_failed", scope, "pass", site=feed.name)]
        latest = events[0]
        failures = [e for e in events if e.status.upper() == "FAILED"]
        if latest.status.upper() != "FAILED":
            return [Outcome("producer_failed", scope, "pass", site=feed.name, details={"failures_24h": len(failures)})]
        return [Outcome("producer_failed", scope, "fail", self._sev("producer_failed", "critical"),
                        f"{feed.producer.name or latest.name} failed at {latest.at.replace(tzinfo=UTC).astimezone(self.tz):%H:%M}: {latest.message[:240]}",
                        site=feed.name, details={"failures_24h": len(failures), "error": latest.message, "producer": feed.producer.name})]

    def feed_schema_drift(self) -> list[Outcome]:
        results = []
        for table, feed in {f.table: f for f in self.ctx.cfg.feeds if "schema_drift" in feed_checks(f)}.items():
            oid = table.lower()
            scope = f"output:{oid}"
            current = [asdict(c) for c in _source(self.ctx, feed).read_schema(table)]
            snap = self.ctx.session.scalars(
                select(SchemaSnapshot).where(SchemaSnapshot.project_id == self.ctx.project.id, SchemaSnapshot.output_id == oid)
                .order_by(SchemaSnapshot.captured_at.desc(), SchemaSnapshot.id.desc()).limit(1)).first()
            fp = schema_fingerprint(current)
            if snap is None:
                self.ctx.session.add(SchemaSnapshot(project_id=self.ctx.project.id, output_id=oid, columns=current, fingerprint=fp, captured_at=self.now))
                results.append(Outcome("schema_drift", scope, "pass", message="Schema recorded as the baseline."))
                continue
            if snap.fingerprint == fp:
                results.append(Outcome("schema_drift", scope, "pass"))
                continue
            d = diff_schema(snap.columns, current)
            parts = [f"removed {', '.join(d['removed'])}"] if d["removed"] else []
            parts += [f"retyped {', '.join(r['column'] for r in d['retyped'])}"] if d["retyped"] else []
            parts += [f"added {', '.join(d['added'])}"] if d["added"] else []
            results.append(Outcome("schema_drift", scope, "fail", self._sev("schema_drift", "critical" if d["removed"] or d["retyped"] else "warning"),
                                   f"{table} changed: {'; '.join(parts)}.",
                                   details={**d, "table": table, "output": oid, "baseline_captured_at": iso(snap.captured_at), "current": current}))
        return results


def weekday_name(dt: datetime, tz: ZoneInfo) -> str:
    return WEEKDAYS[local_weekday(dt, tz)]
