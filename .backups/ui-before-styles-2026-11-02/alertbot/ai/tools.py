"""Read-only investigation tools.

Shared by the rule-based investigator (today) and the LangGraph agent (once a
model is chosen). Guardrails, enforced here rather than in a prompt:

* every tool is bound to one project's context; there is no project argument,
  so the agent cannot reach another project;
* tools run fixed, parameterised queries through the connectors; there is no
  free-form SQL anywhere;
* every call is written to `agent_tool_calls` against the incident.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..db import AgentToolCall, Incident, MetricObservation, Run, SchemaSnapshot
from ..engine.baselines import band_for
from ..engine.context import ProjectContext
from ..timeutil import WEEKDAYS, iso, local_weekday

MAX_ITEMS = 25


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


class ToolBox:
    """The investigator's tools for one incident."""

    SPECS: dict[str, str] = {
        "get_trigger_rows": "Trigger rows for a site over the last N hours, with status and pipeline run ids.",
        "get_adf_run": "Pipeline run status, start/end, duration and parameters for a run id.",
        "get_activity_runs": "Each activity of a pipeline run with its output and error text (HTTP codes, rows copied).",
        "query_metric": "Recent values of a metric (row_count, null_rate, duplicate_rate, duration_s) for a site, newest first.",
        "get_baseline": "The learned normal range for a metric on a weekday (0=Monday), with the number of samples behind it.",
        "compare_schema": "Column differences between the output table now and the last accepted schema.",
        "recent_incidents": "Similar past incidents for this project and how they ended.",
    }

    def __init__(self, ctx: ProjectContext, incident: Incident, agent: str):
        self.ctx = ctx
        self.incident = incident
        self.agent = agent

    # dispatcher with audit log ---------------------------------------------------

    def call(self, tool: str, **args: Any) -> Any:
        if tool not in self.SPECS:
            raise ValueError(f"unknown tool {tool}")
        started = time.perf_counter()
        error: str | None = None
        result: Any = None
        try:
            result = _jsonable(getattr(self, f"_{tool}")(**args))
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.ctx.session.add(
                AgentToolCall(
                    incident_id=self.incident.id,
                    agent=self.agent,
                    tool=tool,
                    args=_jsonable(args),
                    result=result,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    error=error,
                    at=self.ctx.now,
                )
            )

    # tools -------------------------------------------------------------------------

    def _get_trigger_rows(self, site: str, hours: int = 48) -> list[dict[str, Any]]:
        since = self.ctx.now - timedelta(hours=min(int(hours), 24 * 14))
        rows = self.ctx.session.scalars(
            select(Run)
            .where(Run.project_id == self.ctx.project.id, Run.site == site, Run.requested_at >= since)
            .order_by(Run.requested_at.desc())
            .limit(MAX_ITEMS)
        )
        return [
            {"trigger_id": r.trigger_id, "requested_at": r.requested_at, "trigger_status": r.trigger_status,
             "run_id": r.run_id, "run_status": r.run_status, "rows_loaded": r.rows_loaded}
            for r in rows
        ]

    def _get_adf_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.ctx.connectors.pipeline.get_runs([run_id]).get(run_id)
        if not run:
            return None
        # a run's parameters can carry values (file names, ids, dates passed in): only their names go to a model
        return {**{k: v for k, v in asdict(run).items() if k != "parameters"}, "parameter_names": sorted(run.parameters or {})}

    def _get_activity_runs(self, run_id: str) -> list[dict[str, Any]]:
        """Each step's status, timing, error and COUNTS only. A step's raw output can hold real data (a Lookup returns
        rows, a Web step an API response), so it never goes to a model."""
        from ..connectors.adf import _step_facts
        from ..dq.privacy import scrub_for_llm

        def safe(a: Any) -> dict[str, Any]:
            d = {k: v for k, v in asdict(a).items() if k not in ("output", "input", "error")}
            d["output"] = {k: v for k, v in (a.output or {}).items() if v is None or isinstance(v, (int, float, bool))}  # counts, codes
            d["error"] = {**a.error, "message": scrub_for_llm(str(a.error.get("message", "")))} if a.error else None
            d["facts"] = _step_facts(a)
            return d

        return [safe(a) for a in self.ctx.connectors.pipeline.get_activity_runs(run_id)][:MAX_ITEMS]

    def _query_metric(self, site: str, metric: str, field: str = "", output_id: str | None = None, last: int = 10) -> list[dict[str, Any]]:
        output_id = output_id or self.ctx.cfg.primary_output_id
        rows = self.ctx.session.scalars(
            select(MetricObservation)
            .where(
                MetricObservation.project_id == self.ctx.project.id,
                MetricObservation.output_id == output_id,
                MetricObservation.site == site,
                MetricObservation.metric == metric,
                MetricObservation.field == field,
            )
            .order_by(MetricObservation.observed_at.desc())
            .limit(min(int(last), MAX_ITEMS))
        )
        return [{"observed_at": o.observed_at, "weekday": WEEKDAYS[o.weekday], "value": o.value, "run": o.run_ref} for o in rows]

    def _get_baseline(self, site: str, metric: str, field: str = "", weekday: int | None = None, output_id: str | None = None) -> dict[str, Any] | None:
        output_id = output_id or self.ctx.cfg.primary_output_id
        if weekday is None:
            weekday = local_weekday(self.ctx.now, self.ctx.settings.tz)
        band = band_for(self.ctx, output_id, site, metric, field, int(weekday), before=self.ctx.now + timedelta(seconds=1))
        if band is None:
            return None
        return {**asdict(band), "weekday_name": WEEKDAYS[band.weekday] if band.weekday >= 0 else "all days"}

    def _compare_schema(self, output_id: str | None = None) -> dict[str, Any]:
        from ..engine.checks import diff_schema

        cfg = self.ctx.cfg
        if not cfg.schema_tables:
            return {"note": "This project has no tracked table schemas."}
        if cfg.kind == "feeds":
            output_id = output_id or next(iter(cfg.schema_tables))
            source_id, table = cfg.schema_tables[output_id]
            current = [asdict(c) for c in self.ctx.connectors.source(source_id).read_schema(table)]  # type: ignore[arg-type]
        else:
            output_id = output_id or cfg.outputs[0].id
            out = next(o for o in cfg.outputs if o.id == output_id)
            current = [asdict(c) for c in self.ctx.connectors.source(out.source).read_schema(out)]
        snap = self.ctx.session.scalars(
            select(SchemaSnapshot)
            .where(SchemaSnapshot.project_id == self.ctx.project.id, SchemaSnapshot.output_id == output_id)
            .order_by(SchemaSnapshot.captured_at.desc(), SchemaSnapshot.id.desc())
            .limit(1)
        ).first()
        if snap is None:
            return {"baseline": None, "current": current, "added": [], "removed": [], "retyped": []}
        return {"baseline_captured_at": snap.captured_at, **diff_schema(snap.columns, current)}

    def _recent_incidents(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.ctx.session.scalars(
            select(Incident)
            .where(
                Incident.project_id == self.ctx.project.id,
                Incident.scope == self.incident.scope,
                Incident.id != self.incident.id,
            )
            .order_by(Incident.opened_at.desc())
            .limit(min(int(limit), 10))
        )
        return [
            {"id": i.id, "title": i.title, "opened_at": i.opened_at, "status": i.status, "resolved_at": i.resolved_at,
             "likely_cause": i.likely_cause, "checks": i.checks}
            for i in rows
        ]
