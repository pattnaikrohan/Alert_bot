"""The data-quality workflow (LangGraph):

    START → validate_request → load_config ─┬─ collect: select_checks → execute → validate_aggregates → END
                                            └─ analyze: evaluate → analyze → report → END

`collect` runs inside every bot cycle (and on demand) and hands validated profiles to the bot's judging, which
compares them with each table's learned normal and opens incidents. `analyze` runs after a check on demand: it turns
the judged results into the strict AI payload (numbers and identifiers only, `privacy.AiPayload`), asks the model for
an assessment and stores the report. The model never sees a tool, a query, a credential or a value from a table.

Runtime objects (database clients, per-table runs) live in a per-run closure, never in the graph state, so two runs
can't mix their results and nothing secret is ever part of the state.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..db import BotState, CheckResult, DqMapping, MetricObservation
from ..timeutil import iso
from .client import DbClient, DbError
from .privacy import AiPayload, RawDataRejected, sanitize_error, scrub_for_llm
from .service import (
    PlanError,
    TableRun,
    as_utc,
    build_plan,
    canonical,
    client_of,
    settings_of,
    today_start,
)
from .tools import make_tools, select_checks
from .vault import VaultError

log = logging.getLogger(__name__)


class DqState(TypedDict, total=False):
    run_id: str
    project: str
    stage: Literal["collect", "analyze"]
    mapping_ids: list[int]
    tables: dict[int, dict[str, Any]]    # mapping id → what it is, which checks, status, error
    results: dict[int, dict[str, Any]]   # mapping id → tool → numbers
    metrics: dict[int, dict[str, Any]]   # mapping id → flat approved metrics (numbers only)
    profiled: list[Any]                  # engine.quality.Profiled, for the cycle's judging
    errors: list[str]
    payload: dict[str, Any]
    analysis: dict[str, Any] | None
    report: dict[str, Any]


class Request(BaseModel):
    model_config = ConfigDict(extra="ignore")
    run_id: str = Field(pattern=r"^[A-Za-z0-9_\-]{6,64}$")
    project: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,100}$")
    stage: Literal["collect", "analyze"]
    mapping_ids: list[int] = Field(max_length=500)


@dataclass
class _Run:
    ctx: Any
    mappings: dict[int, DqMapping] = field(default_factory=dict)
    clients: dict[int, DbClient] = field(default_factory=dict)
    runs: dict[int, TableRun] = field(default_factory=dict)
    columns: dict[int, list[tuple[str, str]]] = field(default_factory=dict)


_SAFE_KEY = re.compile(r"^[^\x00-\x1f\x7f'\"\[\];{}<>]{1,200}$")


def flatten(results: dict[str, Any]) -> dict[str, float | int | None]:
    """Tool outputs → one flat dict of numbers. A string anywhere (other than the freshness timestamp) fails closed."""
    out: dict[str, float | int | None] = {}

    def walk(prefix: str, v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if not isinstance(k, str) or not _SAFE_KEY.match(k):
                    raise RawDataRejected("a metric name the bot can't use safely")
                walk(f"{prefix}.{k}" if prefix else k, x)
        elif v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)):
            out[prefix] = v
        elif isinstance(v, str) and prefix.endswith("latest_record_at"):
            return  # a timestamp we computed: kept in the report, not in the AI's numbers
        else:
            raise RawDataRejected(f"{prefix} isn't a number")

    for tool, v in results.items():
        walk(tool.removeprefix("check_"), v)
    return out


def build_graph(run: _Run) -> Any:
    ctx = run.ctx
    tz = ctx.settings.tz

    def validate_request(state: DqState) -> DqState:
        req = Request.model_validate(state)
        if req.project != ctx.key:
            raise ValueError("the request is for another project")
        return {"errors": [], "tables": {}, "results": {}, "metrics": {}, "profiled": []}

    def load_config(state: DqState) -> DqState:
        tables: dict[int, dict[str, Any]] = {}
        errors = list(state.get("errors", []))
        rows = ctx.session.scalars(select(DqMapping).where(DqMapping.project_id == ctx.project.id, DqMapping.id.in_(state["mapping_ids"] or [-1])))
        for m in rows:
            c = m.connection
            obj = canonical(c, m.schema_name, m.table_name)
            tables[m.id] = {"object": obj, "database": c.database, "schema": m.schema_name, "table": m.table_name, "pipeline": m.pipeline,
                            "connection_id": c.id, "connection_status": c.status, "checks": [], "status": "pending", "error": None}
            run.mappings[m.id] = m
            if state["stage"] == "collect":
                try:
                    run.clients[m.id] = client_of(c)
                except VaultError as exc:
                    tables[m.id].update(status="error", error=str(exc))
                    errors.append(f"{obj}: {exc}")
        return {"tables": tables, "errors": errors}

    def route(state: DqState) -> str:
        return state["stage"]

    def select_checks_node(state: DqState) -> DqState:
        tables, errors = dict(state["tables"]), list(state["errors"])
        start = today_start(ctx.now, tz)
        for mid, client in run.clients.items():
            t = tables[mid]
            try:
                cols = client.columns(t["schema"], t["table"])
                plan = build_plan(client, run.mappings[mid], cols)
                run.columns[mid] = cols
                run.runs[mid] = TableRun(client=client, plan=plan, start=start)
                t["checks"] = select_checks(run.runs[mid])
            except (DbError, PlanError) as exc:
                t.update(status="error", error=str(exc))
                errors.append(f"{t['object']}: {exc}")
        return {"tables": tables, "errors": errors}

    def execute(state: DqState) -> DqState:
        tables, errors = dict(state["tables"]), list(state["errors"])

        def one(mid: int) -> tuple[int, dict[str, Any] | None, str | None]:
            tools = make_tools(run.runs[mid], ctx.now, tz)
            try:
                return mid, {name: tools[name].invoke({}) for name in tables[mid]["checks"]}, None
            except DbError as exc:
                return mid, None, str(exc)
            except RawDataRejected as exc:
                return mid, None, sanitize_error(exc).message
            except Exception as exc:  # noqa: BLE001 - a tool error is reported in our own words only
                log.warning("dq run %s: a check on mapping %s failed (%s)", state["run_id"], mid, type(exc).__name__)
                return mid, None, sanitize_error(exc).message

        results: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for mid, res, err in pool.map(one, list(run.runs)):
                if err:
                    tables[mid].update(status="error", error=err)
                    errors.append(f"{tables[mid]['object']}: {err}")
                else:
                    results[mid] = res or {}
        return {"tables": tables, "errors": errors, "results": results}

    def validate_aggregates(state: DqState) -> DqState:
        """Second line of defence: every result must flatten to numbers. One that doesn't is dropped whole."""
        from ..engine.quality import Column, Profile, Profiled

        tables, errors = dict(state["tables"]), list(state["errors"])
        metrics: dict[int, dict[str, Any]] = {}
        profiled: list[Any] = []
        for mid, res in state["results"].items():
            t = tables[mid]
            try:
                metrics[mid] = flatten(res)
            except RawDataRejected as exc:
                t.update(status="error", error=sanitize_error(exc).message)
                errors.append(f"{t['object']}: {t['error']}")
                continue
            r, m = run.runs[mid], run.mappings[mid]
            s = settings_of(m)
            dups, groups = r.duplicates()
            stats: dict[str, dict[str, float | None]] = {}
            for metric in ("min", "max", "avg", "stddev"):
                for c, v in r.values(metric).items():
                    stats.setdefault(c, {})[metric] = v
            profile = Profile(
                table=t["object"], columns=[Column(n, ty) for n, ty in run.columns[mid]], date_column=r.plan.date_column,
                rows_total=r.rows_total(), rows_today=r.rows_today() if r.plan.date_column else None,
                latest=as_utc(r.latest(), tz), duplicate_rows=dups, nulls=r.values("null"), negatives=r.values("negative"),
                rule_fails=r.values("rule"), orphans=r.orphans(), means=r.values("mean"), top_share=r.shares(),
                profiled_rows=r.rows_today(), empties=r.values("empty"), invalid=r.values("invalid"),
                out_of_range=r.values("out_of_range"), stats=stats, duplicate_groups=groups, null_key_rows=r.values("null_key").get(""),
                days_recent=r.partitions()[0], days_before=r.partitions()[1])
            profiled.append(Profiled(quality_table(t["object"], m.pipeline, r.plan, s), m.pipeline, profile.columns, profile))
            t["status"] = "checked"
        return {"tables": tables, "errors": errors, "metrics": metrics, "profiled": profiled}

    def evaluate(state: DqState) -> DqState:
        """The judged results of each table (written by the cycle), as the strict AI payload: every check with its
        measured value and normal range, the pipeline's recent runs, and the empty share of each column."""
        out = []
        for t in state["tables"].values():
            obj, pipe = t["object"], t["pipeline"]
            latest: dict[str, CheckResult] = {}
            for scope in (f"data:{obj}", f"table:{obj}", f"site:{pipe}"):
                for r in ctx.session.scalars(select(CheckResult).where(
                        CheckResult.project_id == ctx.project.id, CheckResult.scope == scope, CheckResult.status.in_(("pass", "fail")))
                        .order_by(CheckResult.created_at.desc(), CheckResult.id.desc()).limit(300)):
                    if scope.startswith("site:") and r.check not in PIPELINE_CHECKS:
                        continue
                    key = r.check + ":" + str((r.details or {}).get("rule") or (r.details or {}).get("reference") or "")
                    latest.setdefault(key, r)
            obs = list(ctx.session.scalars(select(MetricObservation).where(
                MetricObservation.project_id == ctx.project.id, MetricObservation.output_id == f"dq:{obj}")
                .order_by(MetricObservation.observed_at.desc()).limit(600)))
            last_ref = obs[0].run_ref if obs else None
            metrics = {f"{o.metric}{'.' + o.field if o.field else ''}": round(o.value, 6) for o in obs if o.run_ref == last_ref}
            empty = {k.split(".", 1)[1]: round(v * 100, 2) for k, v in metrics.items() if k.startswith("null_rate.") and v is not None}
            empty = dict(sorted(empty.items(), key=lambda kv: -(kv[1] or 0))[:25])
            fails = [r for r in latest.values() if r.status == "fail"]
            status = ("UNAVAILABLE" if t["connection_status"] == "error" and not latest else "LEARNING" if not latest
                      else "CRITICAL" if any(r.severity == "critical" for r in fails) else "WARNING" if fails else "OK")
            checks = [{"check": r.check, "status": r.status, "severity": r.severity if r.status == "fail" else None,
                       "field": str((r.details or {}).get("rule") or (r.details or {}).get("reference") or "")[:120],
                       "message": (r.message or "")[:500], "actual": r.actual, "expected_low": r.expected_low, "expected_high": r.expected_high}
                      for r in latest.values()]
            out.append({"object": obj, "database": t["database"], "schema": t["schema"], "table": t["table"], "pipeline": pipe,
                        "quality_status": status, "quality_metrics": {k: v for k, v in metrics.items() if _SAFE_KEY.match(k)},
                        "checks": checks, "pipeline_stats": pipeline_stats(ctx.session, ctx.project.id, pipe, ctx.now),
                        "empty_pct_by_column": {k: v for k, v in empty.items() if _SAFE_KEY.match(k)}})
        payload = AiPayload.model_validate({"run_id": state["run_id"], "project": ctx.key, "tables": out})  # fails closed
        return {"payload": payload.model_dump(by_alias=True)}

    def analyze(state: DqState) -> DqState:
        """The model explains the aggregate results. Without a model (or if its answer is unusable) the rules do."""
        from ..ai.llm import AIUnavailable, client

        payload = state["payload"]
        if not payload["tables"]:
            return {"analysis": None}
        rules = explain_with_rules(payload)
        if not any(t["quality_status"] in ("WARNING", "CRITICAL", "OK") for t in payload["tables"]):
            return {"analysis": rules}
        try:
            data, res = client().chat_json(SYSTEM, json.dumps(payload, default=str), max_tokens=3500)
        except AIUnavailable as exc:
            return {"analysis": {**rules, "note": f"AI unavailable ({exc}); explained by the bot's rules."}}
        except Exception as exc:  # noqa: BLE001
            return {"analysis": {**rules, "note": f"The AI couldn't answer ({type(exc).__name__}); explained by the bot's rules."}}
        ai = clean_analysis(data, payload)
        if not ai["tables"]:
            return {"analysis": {**rules, "note": "The AI's answer was unusable; explained by the bot's rules."}}
        by_obj = {t["table"]: t for t in ai["tables"]}
        merged = [by_obj.get(t["table"], t) for t in rules["tables"]]  # any table the model skipped keeps the rules' account
        return {"analysis": {"summary": ai["summary"] or rules["summary"], "tables": merged, "model": res.model, "source": "ai"}}

    def report(state: DqState) -> DqState:
        rep = {"run_id": state["run_id"], "at": iso(ctx.now), "project": ctx.key, "tables": state["payload"]["tables"],
               "analysis": state.get("analysis")}
        row = ctx.session.get(BotState, f"dq-report:{ctx.key}")
        if row is None:
            row = BotState(key=f"dq-report:{ctx.key}")
            ctx.session.add(row)
        row.value, row.updated_at = rep, ctx.now
        ctx.session.flush()
        return {"report": rep}

    g = StateGraph(DqState)
    for name, fn in (("validate_request", validate_request), ("load_config", load_config), ("select_checks", select_checks_node),
                     ("execute", execute), ("validate_aggregates", validate_aggregates), ("evaluate", evaluate),
                     ("analyze", analyze), ("report", report)):
        g.add_node(name, fn)
    g.add_edge(START, "validate_request")
    g.add_edge("validate_request", "load_config")
    g.add_conditional_edges("load_config", route, {"collect": "select_checks", "analyze": "evaluate"})
    g.add_edge("select_checks", "execute")
    g.add_edge("execute", "validate_aggregates")
    g.add_edge("validate_aggregates", END)
    g.add_edge("evaluate", "analyze")
    g.add_edge("analyze", "report")
    g.add_edge("report", END)
    return g.compile()


SYSTEM = scrub_for_llm("""You are a senior data reliability engineer writing for the people who own these tables.
You receive ONLY aggregate data-quality results (counts, percentages, approved statistics, pass/fail checks with their
measured value and normal range, and the loading pipeline's recent runs). You never see rows or values and must not ask for them.

For each table: decide a verdict, then explain clearly and concretely in plain English what the numbers show, why it matters,
the most likely causes in data-engineering terms (late or partial load, duplicate or re-run load, upstream schema change,
source outage, bad extract, filter change, unit change, backfill…), who or what is affected, and the exact next steps.
Quote the real numbers from the input (e.g. "1,240 rows today against a normal of ~12,400"). Never invent a number.
Check names: row_count, null_spike (empty values), duplicates, freshness, schema_drift, value_range (negatives),
out_of_range, invalid_type, distribution_shift, business_rule, referential_integrity, missing_partitions,
source_available, run_failed, run_too_slow (pipeline duration), missed_run, rows_skipped, empty_site (a load with no rows).

Answer as JSON only:
{"summary": "2-3 sentences across all tables",
 "tables": [{"table": "<the input's object>", "verdict": "healthy" | "needs attention" | "broken",
   "headline": "one sentence, the single most important point",
   "explanation": "3-5 sentences, plain English, with the numbers",
   "findings": [{"check": "<check name>", "title": "short", "detail": "one sentence with the numbers"}],
   "likely_causes": [{"cause": "...", "confidence": "high" | "medium" | "low"}],
   "impact": "who / what is affected",
   "next_steps": ["imperative, specific"],
   "fine": ["short phrases for what is healthy"]}]}""")


PIPELINE_CHECKS = ("run_failed", "run_too_slow", "missed_run", "sla_risk")
CHECK_TITLES = {
    "row_count": "Row count", "null_spike": "Empty values", "duplicates": "Duplicates", "freshness": "Freshness",
    "schema_drift": "Schema change", "value_range": "Negative values", "out_of_range": "Values out of range",
    "invalid_type": "Badly typed values", "distribution_shift": "Distribution change", "business_rule": "Business rule",
    "referential_integrity": "Referential integrity", "missing_partitions": "Missing days", "source_available": "Source availability",
    "run_failed": "Pipeline failure", "run_too_slow": "Pipeline duration", "missed_run": "Missed run", "rows_skipped": "Skipped rows",
    "empty_site": "Empty load", "sla_risk": "Deadline at risk",
}
_CAUSES = {
    "run_failed": ("The pipeline run itself failed, so the table didn't get its new data.", "high",
                   "Open the failed run in Runs, fix the failing step, then re-run the pipeline."),
    "empty_site": ("The source delivered nothing: the file or extract was missing or empty when the copy ran.", "high",
                   "Check the source folder or API for today's delivery, then re-run the load."),
    "row_count": ("A partial or late load: fewer (or more) rows arrived than this table normally gets.", "medium",
                  "Compare today's source file or extract with yesterday's and re-run if it was incomplete."),
    "freshness": ("New data hasn't arrived on time: the load ran late, failed, or wrote no new rows.", "medium",
                  "Check the pipeline's last run and the source's delivery time."),
    "missing_partitions": ("Loads were skipped on some days.", "high", "Backfill the missing days and check the trigger schedule."),
    "duplicates": ("The same data was loaded twice: a re-run without clean-up or a file delivered twice.", "medium",
                   "Find the duplicate load in Runs, remove the repeated rows, and make the load idempotent (merge, or truncate first)."),
    "null_spike": ("A column stopped being filled: an upstream mapping or source change.", "medium",
                   "Compare the source columns with the pipeline's mapping for the columns named above."),
    "schema_drift": ("The table's columns changed: an upstream schema change or a deployment.", "high",
                     "Confirm the change was intended, update the pipeline mapping, then accept the new schema."),
    "value_range": ("Values outside their usual sign appeared: a unit or sign change, or bad source values.", "medium",
                    "Check the source for the columns named above."),
    "out_of_range": ("Values outside their allowed range arrived from the source.", "medium", "Check the source for the columns named above."),
    "invalid_type": ("Text that should be numbers or dates arrived in a different format.", "medium",
                     "Check the source format for the columns named above (separators, date format)."),
    "distribution_shift": ("The data's mix changed: a filter, a new region or product, or a partial extract.", "low",
                           "Check with the source owner whether a change in volume or mix is expected."),
    "business_rule": ("Rows break an agreed business rule.", "medium", "Check the rows breaking the rule in the source."),
    "referential_integrity": ("Rows reference keys that don't exist yet: the referenced table loads later, or is incomplete.", "medium",
                              "Check that the referenced table loaded first and completely."),
    "source_available": ("The bot couldn't reach the database to check the table.", "high",
                         "Check the connection on the Databases page (server, firewall, password)."),
    "run_too_slow": ("The pipeline took much longer than usual: more data, a slower source, or a busy integration runtime.", "low",
                     "Compare the run's step durations with a normal run."),
    "missed_run": ("The pipeline didn't run when expected.", "high", "Check the trigger is started and its schedule."),
    "rows_skipped": ("The copy skipped incompatible rows.", "medium", "Look at the copy's skipped-rows log."),
}


def explain_with_rules(payload: dict[str, Any]) -> dict[str, Any]:
    """The same explanation shape as the model's, from the checks alone: always available, never invents anything."""
    tables, bad = [], 0
    for t in payload["tables"]:
        fails = sorted((c for c in t["checks"] if c["status"] == "fail"), key=lambda c: c["severity"] != "critical")
        fine = sorted({CHECK_TITLES.get(c["check"], c["check"]) for c in t["checks"] if c["status"] == "pass"})
        verdict = "broken" if t["quality_status"] == "CRITICAL" else "needs attention" if fails else "healthy"
        bad += bool(fails)
        name = f"{t['schema']}.{t['table']}"
        if t["quality_status"] == "LEARNING":
            headline = f"{name} was checked for the first time; the bot is learning its normal values."
        elif not fails:
            headline = f"{name} passed every check."
        else:
            headline = fails[0]["message"].split(": ", 1)[-1] if fails[0]["message"] else f"{CHECK_TITLES.get(fails[0]['check'], fails[0]['check'])} failed."
        causes, steps, seen = [], [], set()
        for c in fails:
            if c["check"] in _CAUSES and c["check"] not in seen:
                seen.add(c["check"])
                cause, conf, step = _CAUSES[c["check"]]
                causes.append({"cause": cause, "confidence": conf})
                steps.append(step)
        ps = t.get("pipeline_stats") or {}
        parts = [headline]
        if len(fails) > 1:
            parts.append(f"{len(fails)} checks failed in total; the details are below.")
        if ps.get("failed_7d"):
            parts.append(f"The pipeline {t['pipeline']} failed {int(ps['failed_7d'])} time(s) in the last 7 days.")
        tables.append({"table": t["object"], "verdict": verdict, "headline": headline, "explanation": " ".join(parts),
                       "findings": [{"check": c["check"], "title": CHECK_TITLES.get(c["check"], c["check"]) + (f": {c['field']}" if c["field"] else ""),
                                     "detail": c["message"] or "Failed."} for c in fails[:8]],
                       "likely_causes": causes[:4], "impact": "Reports and pipelines that read this table see the problems above." if fails else "",
                       "next_steps": steps[:5], "fine": fine[:12]})
    summary = (f"{bad} of {len(tables)} table(s) need attention." if bad else f"All {len(tables)} table(s) passed their checks.")
    return {"summary": summary, "tables": tables, "model": None, "source": "rules"}


def clean_analysis(data: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only well-formed, length-limited fields of the model's answer, for tables that were in the input."""
    known = {t["object"] for t in payload["tables"]}

    def s(v: Any, n: int) -> str:
        return str(v or "").strip()[:n]

    tables = []
    for t in (data.get("tables") or [])[:50] if isinstance(data, dict) else []:
        if not isinstance(t, dict) or s(t.get("table"), 300) not in known:
            continue
        verdict = s(t.get("verdict"), 20).lower()
        tables.append({
            "table": s(t.get("table"), 300), "verdict": verdict if verdict in ("healthy", "needs attention", "broken") else "needs attention",
            "headline": s(t.get("headline"), 300), "explanation": s(t.get("explanation"), 1600),
            "findings": [{"check": s(f.get("check"), 40), "title": s(f.get("title"), 120), "detail": s(f.get("detail"), 400)}
                         for f in (t.get("findings") or [])[:10] if isinstance(f, dict)],
            "likely_causes": [{"cause": s(c.get("cause") if isinstance(c, dict) else c, 300),
                               "confidence": (s(c.get("confidence"), 10).lower() if isinstance(c, dict) else "medium") or "medium"}
                              for c in (t.get("likely_causes") or [])[:5]],
            "impact": s(t.get("impact"), 500), "next_steps": [s(x, 300) for x in (t.get("next_steps") or [])[:6]],
            "fine": [s(x, 80) for x in (t.get("fine") or [])[:12]],
        })
    return {"summary": s((data or {}).get("summary") if isinstance(data, dict) else "", 900), "tables": tables}


def pipeline_stats(session: Any, project_id: int, pipeline: str, now: Any) -> dict[str, float | int | None]:
    """The loading pipeline's last 7 days, as numbers: runs, failures, retries (a failure followed by a success the
    same day), the last and the normal duration, hours since the last successful load."""
    from statistics import median

    from sqlalchemy import or_

    from ..db import Run

    runs = list(session.scalars(select(Run).where(Run.project_id == project_id, or_(Run.pipeline == pipeline, Run.site == pipeline),
                                                  Run.requested_at >= now - timedelta(days=30)).order_by(Run.requested_at)))
    week = [r for r in runs if r.requested_at >= now - timedelta(days=7)]
    failed = [r for r in week if r.run_status in ("Failed", "Cancelled")]
    retries = sum(1 for f in failed if any(r.run_status == "Succeeded" and f.requested_at < r.requested_at <= f.requested_at + timedelta(hours=24)
                                            for r in week))
    ok = [r for r in runs if r.run_status == "Succeeded" and r.duration_s]
    last = runs[-1] if runs else None
    last_ok = max((r.ended_at or r.requested_at for r in runs if r.run_status == "Succeeded"), default=None)
    return {"runs_7d": len(week), "failed_7d": len(failed), "retries_7d": retries,
            "last_run_failed": (1 if last.run_status in ("Failed", "Cancelled") else 0) if last else None,
            "last_duration_min": round(last.duration_s / 60, 2) if last and last.duration_s else None,
            "normal_duration_min": round(median(r.duration_s for r in ok[-14:]) / 60, 2) if ok else None,
            "hours_since_last_load": round((now - last_ok).total_seconds() / 3600, 2) if last_ok else None}


def quality_table(obj: str, pipeline: str, plan: Any, s: Any) -> Any:
    """The mapping's settings in the shape the bot's judging understands (rules as display text only: never run)."""
    from ..config import QualityReference, QualityRule, QualityTable

    rules = []
    for r in s.rules:
        try:
            rules.append(QualityRule(name=r.name, sql=r.display(), severity=r.severity, max_fail_rate=r.max_fail_rate))
        except ValueError:
            rules.append(QualityRule(name=r.name, sql=r.name, severity=r.severity, max_fail_rate=r.max_fail_rate))
    return QualityTable(table=obj, pipeline=pipeline, date_column=plan.date_column or "", key=list(s.key), required=list(s.required),
                        freshness_hours=s.freshness_hours, distribution=list(plan.categories), rules=rules,
                        references=[QualityReference(column=c, table=f"{rs}.{rt}", ref_column=rc) for c, rs, rt, rc in plan.references])


def run_dq(ctx: Any, mapping_ids: list[int], stage: Literal["collect", "analyze"] = "collect", run_id: str | None = None) -> DqState:
    """One run of the workflow for a project's mappings. Each call has its own id and its own runtime objects."""
    run = _Run(ctx=ctx)
    state: DqState = {"run_id": run_id or f"dq-{uuid.uuid4().hex[:16]}", "project": ctx.key, "stage": stage, "mapping_ids": list(mapping_ids)}
    return build_graph(run).invoke(state)
