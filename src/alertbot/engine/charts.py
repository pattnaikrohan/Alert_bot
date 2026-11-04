"""Charts people ask for in words, computed from the bot's own records (runs, loads, incidents, checks, alerts).

The AI only turns a request into a `ChartSpec`: a metric from CATALOGUE, a period, a grouping and filters. It never
writes a query, and nothing here reads a monitored table: every number comes from what the bot already stored about
the project. The same spec is recomputed each time a pinned chart is shown, so it stays current.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import CheckResult, Incident, MetricObservation, Notification, Project, Run

Group = Literal["none", "day", "week", "month", "pipeline", "table", "status", "check", "severity"]
TIME = ("day", "week", "month")


@dataclass(frozen=True)
class Metric:
    label: str
    unit: str
    describe: str
    groups: tuple[str, ...]
    kind: Literal["count", "average", "rate", "current"]
    needs: tuple[str, ...] = ()  # required filters


CATALOGUE: dict[str, Metric] = {
    "runs": Metric("Pipeline runs", "runs", "Number of pipeline runs started", ("day", "week", "month", "pipeline", "status"), "count"),
    "failed_runs": Metric("Failed runs", "runs", "Pipeline runs that failed or were cancelled", ("day", "week", "month", "pipeline"), "count"),
    "succeeded_runs": Metric("Successful runs", "runs", "Pipeline runs that succeeded", ("day", "week", "month", "pipeline"), "count"),
    "success_rate": Metric("Success rate", "%", "Share of finished runs that succeeded", ("day", "week", "month", "pipeline"), "rate"),
    "avg_duration_min": Metric("Average run duration", "min", "Average duration of successful runs, in minutes",
                               ("day", "week", "month", "pipeline"), "average"),
    "max_duration_min": Metric("Longest run", "min", "Longest successful run, in minutes", ("day", "week", "month", "pipeline"), "average"),
    "rows_loaded": Metric("Rows loaded", "rows", "Rows the pipelines' copy steps wrote into tables",
                          ("day", "week", "month", "pipeline", "table"), "count"),
    "incidents_opened": Metric("Incidents opened", "incidents", "Problems the bot opened", ("day", "week", "month", "severity"), "count"),
    "incidents_resolved": Metric("Incidents resolved", "incidents", "Problems that were fixed", ("day", "week", "month", "severity"), "count"),
    "open_incidents": Metric("Open incidents", "incidents", "Problems open right now", ("severity",), "current"),
    "time_to_resolve_h": Metric("Time to resolve", "h", "Average hours from an incident opening to it being resolved",
                                ("day", "week", "month", "severity"), "average"),
    "checks_failed": Metric("Failed checks", "checks", "Checks that failed (pipeline, load and data-quality checks)",
                            ("day", "week", "month", "check", "table"), "count"),
    "table_rows": Metric("Rows per table (data checks)", "rows", "Rows that arrived today in a mapped table, at each data check",
                         ("day", "week", "month", "table"), "average"),
    "empty_pct": Metric("Empty values", "%", "Share of empty values in one column of a mapped table", ("day", "week", "month"), "average",
                        needs=("table", "column")),
    "freshness_h": Metric("Data age", "h", "How old the newest row of a mapped table was, at each data check", ("day", "week", "month", "table"),
                          "average"),
    "alerts_sent": Metric("Alerts sent", "alerts", "Alerts the bot sent to Teams / email", ("day", "week", "month"), "count"),
}


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pipeline: str | None = Field(default=None, max_length=200)
    table: str | None = Field(default=None, max_length=300)
    column: str | None = Field(default=None, max_length=128)
    status: Literal["succeeded", "failed"] | None = None
    severity: Literal["warning", "critical"] | None = None


class ChartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=90)
    viz: Literal["stat", "bar", "hbar", "line", "area", "pie", "donut", "gauge", "table"]
    metric: str
    period_days: int = Field(default=30, ge=1, le=365)
    group_by: Group = "none"
    filters: Filters = Field(default_factory=Filters)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        return " ".join(v.split())

    @model_validator(mode="after")
    def _fits(self) -> ChartSpec:
        m = CATALOGUE.get(self.metric)
        if m is None:
            raise ValueError(f"unknown metric {self.metric!r}")
        if self.group_by != "none" and self.group_by not in m.groups:
            raise ValueError(f"{m.label} can be grouped by {', '.join(m.groups)}, not {self.group_by}")
        for f in m.needs:
            if not getattr(self.filters, f):
                raise ValueError(f"{m.label} needs a {f}")
        categorical = [g for g in ("pipeline", "status", "severity", "check", "table") if g in m.groups]
        if self.viz in ("pie", "donut") and (self.group_by == "none" or (self.group_by in TIME and categorical)):
            if categorical:
                self.group_by = categorical[0]  # slices are parts of a whole: pipelines, statuses…
            else:
                self.viz = "bar"
        if self.viz == "gauge" and m.unit != "%":
            self.viz = "stat"  # a gauge needs a 0-100 scale
        if self.viz == "gauge":
            self.group_by = "none"
        if self.viz in ("bar", "hbar", "line", "area", "table", "pie", "donut") and self.group_by == "none":
            self.group_by = "day" if "day" in m.groups else m.groups[0]
        if self.viz in ("line", "area") and self.group_by not in TIME:
            self.viz = "bar"
        return self


class ChartError(ValueError):
    """Shown to the person as is."""


def _bucket(at: datetime, group: str) -> tuple[str, str]:
    """(sort key, label) for a time bucket."""
    if group == "week":
        start = (at - timedelta(days=at.weekday())).date()
        return start.isoformat(), f"wk {start:%d %b}"
    if group == "month":
        return f"{at:%Y-%m}", f"{at:%b %Y}"
    return at.date().isoformat(), f"{at:%d %b}"


def _time_axis(since: datetime, now: datetime, group: str) -> list[tuple[str, str]]:
    keys: dict[str, str] = {}
    d = since
    while d <= now:
        k, label = _bucket(d, group)
        keys.setdefault(k, label)
        d += timedelta(days=1)
    return sorted(keys.items())


def _resolve(name: str | None, known: list[str], what: str) -> str | None:
    if not name:
        return None
    exact = next((k for k in known if k.lower() == name.lower()), None)
    if exact:
        return exact
    short = [k for k in known if k.lower().split(".")[-1] == name.lower().split(".")[-1]]
    if len(short) == 1:
        return short[0]
    loose = [k for k in known if name.lower() in k.lower()]
    if len(loose) == 1:
        return loose[0]
    raise ChartError(f"There's no {what} called {name} in this project" + (f" (known: {', '.join(known[:8])})" if known else "") + ".")


def known_names(session: Session, project: Project) -> dict[str, list[str]]:
    """Pipelines and tables this project has records for (names only), to resolve what people type."""
    from ..db import BotState

    inv = session.get(BotState, f"inventory:{project.key}")
    pipes = {i["name"] for i in ((inv.value or {}).get("items") or [])} if inv else set()
    pipes |= {p for p in session.scalars(select(Run.site).where(Run.project_id == project.id).distinct()) if p}  # a run's site is its pipeline
    outs = session.scalars(select(MetricObservation.output_id).where(MetricObservation.project_id == project.id).distinct())
    tables = sorted({o.split(":", 1)[1] for o in outs if o.startswith(("dq:", "table:"))})
    return {"pipelines": sorted(pipes), "tables": tables}


def compute(session: Session, project: Project, spec: ChartSpec, now: datetime) -> dict[str, Any]:
    """The chart's numbers: a headline value (with the previous period for comparison) and a series per group."""
    m = CATALOGUE[spec.metric]
    names = known_names(session, project)
    pipeline = _resolve(spec.filters.pipeline, names["pipelines"], "pipeline")
    table = _resolve(spec.filters.table, names["tables"], "table")
    days = spec.period_days
    since, prev_since = now - timedelta(days=days), now - timedelta(days=2 * days)
    g = spec.group_by

    # each metric yields points: (time, group label, value); counts use value 1 (or rows), averages the measurement
    def points(start: datetime, end: datetime) -> list[tuple[datetime, str, float]]:
        pid = project.id
        if spec.metric in ("runs", "failed_runs", "succeeded_runs", "success_rate", "avg_duration_min", "max_duration_min"):
            q = select(Run).where(Run.project_id == pid, Run.requested_at >= start, Run.requested_at < end)
            if pipeline:
                q = q.where(Run.site == pipeline)
            out = []
            for r in session.scalars(q):
                st = r.run_status or ""
                ok, bad = st == "Succeeded", st in ("Failed", "Cancelled", "TimedOut")
                label = {"pipeline": r.site, "status": st or "Unknown"}.get(g, "")  # site = the pipeline (pipeline = its trigger)
                if spec.metric == "runs":
                    if (spec.filters.status == "failed" and not bad) or (spec.filters.status == "succeeded" and not ok):
                        continue
                    out.append((r.requested_at, label, 1.0))
                elif spec.metric == "failed_runs" and bad or spec.metric == "succeeded_runs" and ok:
                    out.append((r.requested_at, label, 1.0))
                elif spec.metric == "success_rate" and (ok or bad):
                    out.append((r.requested_at, label, 100.0 if ok else 0.0))
                elif spec.metric in ("avg_duration_min", "max_duration_min") and ok and r.duration_s:
                    out.append((r.requested_at, label, r.duration_s / 60))
            return out
        if spec.metric == "rows_loaded":
            q = select(MetricObservation).where(MetricObservation.project_id == pid, MetricObservation.output_id.like("table:%"),
                                                MetricObservation.metric == "row_count", MetricObservation.observed_at >= start,
                                                MetricObservation.observed_at < end)
            if pipeline:
                q = q.where(MetricObservation.site == pipeline)
            if table:
                q = q.where(MetricObservation.output_id == f"table:{table}")
            return [(o.observed_at, {"pipeline": o.site, "table": o.output_id.split(":", 1)[1]}.get(g, ""), o.value) for o in session.scalars(q)]
        if spec.metric in ("incidents_opened", "incidents_resolved", "time_to_resolve_h", "open_incidents"):
            q = select(Incident).where(Incident.project_id == pid)
            if spec.filters.severity:
                q = q.where(Incident.severity == spec.filters.severity)
            out = []
            for i in session.scalars(q):
                label = i.severity if g == "severity" else ""
                if spec.metric == "incidents_opened" and start <= i.opened_at < end:
                    out.append((i.opened_at, label, 1.0))
                elif spec.metric == "incidents_resolved" and i.resolved_at and start <= i.resolved_at < end:
                    out.append((i.resolved_at, label, 1.0))
                elif spec.metric == "time_to_resolve_h" and i.resolved_at and start <= i.resolved_at < end:
                    out.append((i.resolved_at, label, (i.resolved_at - i.opened_at).total_seconds() / 3600))
                elif spec.metric == "open_incidents" and i.status in ("open", "acknowledged") and end >= now:
                    out.append((now, label, 1.0))
            return out
        if spec.metric == "checks_failed":
            q = select(CheckResult).where(CheckResult.project_id == pid, CheckResult.status == "fail", CheckResult.created_at >= start,
                                          CheckResult.created_at < end)
            if table:
                q = q.where(CheckResult.scope.in_((f"data:{table}", f"table:{table}")))
            rows = [r for r in session.scalars(q)
                    if not pipeline or r.scope == f"site:{pipeline}" or (r.details or {}).get("pipeline") == pipeline]
            return [(r.created_at, {"check": r.check, "table": r.scope.split(":", 1)[1]}.get(g, ""), 1.0) for r in rows]
        if spec.metric in ("table_rows", "empty_pct", "freshness_h"):
            metric, fld = {"table_rows": ("row_count", ""), "empty_pct": ("null_rate", spec.filters.column or ""),
                           "freshness_h": ("freshness_hours", "")}[spec.metric]
            q = select(MetricObservation).where(MetricObservation.project_id == pid, MetricObservation.output_id.like("dq:%"),
                                                MetricObservation.metric == metric, MetricObservation.observed_at >= start,
                                                MetricObservation.observed_at < end)
            if table:
                q = q.where(MetricObservation.output_id == f"dq:{table}")
            if fld:
                q = q.where(MetricObservation.field.ilike(fld))
            scale = 100.0 if spec.metric == "empty_pct" else 1.0
            return [(o.observed_at, o.output_id.split(":", 1)[1] if g == "table" else "", o.value * scale) for o in session.scalars(q)]
        if spec.metric == "alerts_sent":
            q = select(Notification).where(Notification.project_id == pid, Notification.status == "sent", Notification.created_at >= start,
                                           Notification.created_at < end)
            return [(n.created_at, "", 1.0) for n in session.scalars(q)]
        raise ChartError(f"{m.label} can't be charted yet.")

    def agg(vals: list[float]) -> float | None:
        if not vals:
            return 0.0 if m.kind in ("count", "current") else None
        if m.kind in ("count", "current"):
            return float(sum(vals))
        if spec.metric == "max_duration_min":
            return max(vals)
        return mean(vals)

    now_pts = points(since, now + timedelta(seconds=1))
    value = agg([v for _, _, v in now_pts])
    previous = None if m.kind == "current" else agg([v for _, _, v in points(prev_since, since)])
    series: list[dict[str, Any]] = []
    if g in TIME:
        grouped: dict[str, list[float]] = defaultdict(list)
        for at, _, v in now_pts:
            grouped[_bucket(at, g)[0]].append(v)
        series = [{"label": label, "value": agg(grouped.get(k, []))} for k, label in _time_axis(since, now, g)]
    elif g != "none":
        grouped = defaultdict(list)
        for _, label, v in now_pts:
            grouped[label or "Unknown"].append(v)
        series = sorted(({"label": k.split(".")[-1] if g == "table" else k, "value": agg(v)} for k, v in grouped.items()),
                        key=lambda x: -(x["value"] or 0))[:15]
    return {"value": None if value is None else round(value, 2), "previous": None if previous is None else round(previous, 2),
            "unit": m.unit, "label": m.label, "series": [{**s, "value": None if s["value"] is None else round(s["value"], 2)} for s in series],
            "period_days": days, "resolved": {"pipeline": pipeline, "table": table}}


def catalogue_text() -> str:
    """The catalogue as the AI reads it."""
    lines = []
    for key, m in CATALOGUE.items():
        lines.append(f"- {key}: {m.describe} (unit {m.unit}; group_by: none, {', '.join(m.groups)}"
                     + (f"; requires filters: {', '.join(m.needs)}" if m.needs else "") + ")")
    return "\n".join(lines)
