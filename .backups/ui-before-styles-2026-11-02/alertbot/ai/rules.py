"""Rule-based investigator: the analysis every incident gets with AI switched off.

It gathers evidence through the same audited read-only tools the AI agent will
use, then applies explicit heuristics. When AI is enabled, this still runs
first, so the agent starts from the facts and a fallback always exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..db import Incident
from ..engine.checks import Outcome
from ..engine.context import ProjectContext


@dataclass
class Analysis:
    summary: str
    impact: str
    likely_cause: str
    suggested_action: str
    confidence: str  # low | medium | high
    evidence: list[dict[str, Any]] = field(default_factory=list)
    source: str = "rules"  # rules | ai


def _scraper(acts: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((a for a in acts if "scrap" in a["name"].lower() or a["type"] == "AzureFunctionActivity"), None)


def _pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def investigate_with_rules(ctx: ProjectContext, incident: Incident, failing: list[Outcome]) -> Analysis:
    from .tools import ToolBox

    if ctx.cfg.kind == "feeds":
        return investigate_feed(ctx, incident, failing)
    if ctx.cfg.kind == "adf":
        if incident.scope.startswith("table:"):
            return investigate_table_load(ctx, incident, failing)
        if incident.scope.startswith("data:"):
            return investigate_data_quality(ctx, incident, failing)
        return investigate_adf(ctx, incident, failing)
    tools = ToolBox(ctx, incident, agent="rules")
    by = {o.check: o for o in failing}
    site = next((o.site for o in failing if o.site), None)
    out_cfg = ctx.cfg.outputs[0]
    evidence: list[dict[str, Any]] = []

    def ev(label: str, detail: str, tool: str) -> None:
        evidence.append({"label": label, "detail": detail, "tool": tool})

    # The run in question, then everything the tools can tell us about it.
    run_id = next((o.details.get("run_id") or o.run_ref for o in failing if (o.details.get("run_id") or o.run_ref)), None)
    acts: list[dict[str, Any]] = []
    if run_id and run_id.count("-") == 4:  # pipeline run ids are GUIDs; trigger ids are not
        try:
            acts = tools.call("get_activity_runs", run_id=run_id)
        except Exception:  # noqa: BLE001 - evidence is best effort
            acts = []
    scraper = _scraper(acts)
    scraper_out = (scraper or {}).get("output") or {}
    http = scraper_out.get("statusCode")
    if scraper:
        if scraper.get("error"):
            ev("Scraper activity failed", scraper["error"].get("message", "")[:300], "get_activity_runs")
        else:
            bits = [f"HTTP {http}" if http else None,
                    f"{scraper_out.get('pagesFetched')} pages fetched" if "pagesFetched" in scraper_out else None,
                    f"{scraper_out.get('itemsParsed')} items parsed" if "itemsParsed" in scraper_out else None,
                    "no activity errors"]
            ev("Scraper activity output", ", ".join(b for b in bits if b), "get_activity_runs")

    history: list[dict[str, Any]] = []
    if site:
        history = tools.call("get_trigger_rows", site=site, hours=24 * 8)
        prior = [h for h in history if h["run_id"] != run_id and h["run_status"]]
        ok = sum(1 for h in prior[:7] if h["run_status"] == "Succeeded")
        if prior:
            ev("Recent runs", f"{ok} of the previous {min(7, len(prior))} runs for {site} succeeded", "get_trigger_rows")

    schema_changed = False
    if any(c in by for c in ("row_count", "null_spike", "empty_site", "schema_drift")):
        schema = tools.call("compare_schema")
        schema_changed = bool(schema.get("added") or schema.get("removed") or schema.get("retyped"))
        ev("Output schema", "changed: " + ", ".join(
            f"{k} {v}" for k, v in schema.items() if k in ("added", "removed", "retyped") and v
        ) if schema_changed else "unchanged since the last accepted snapshot", "compare_schema")

    past = tools.call("recent_incidents", limit=5)
    if past:
        last = past[0]
        ev("Similar past incident", f"#{last['id']} “{last['title']}” ({last['status']})"
           + (f": {last['likely_cause']}" if last.get("likely_cause") else ""), "recent_incidents")

    for o in failing:
        if o.check == "row_count" and o.details.get("baseline"):
            b = o.details["baseline"]
            ev("Baseline", f"{b['basis'].replace('_', ' ')} from {b['samples']} runs, median {b['median']:,.0f} rows", "row_count check")

    summary = " ".join(o.message for o in sorted(failing, key=lambda o: o.check))
    weekday = next((o.details.get("weekday") for o in failing if o.details.get("weekday")), None)

    # Heuristics, most specific first ------------------------------------------------
    cause, action, impact, confidence = "", "", "", "medium"
    rc, ns = by.get("row_count"), by.get("null_spike")
    if "run_failed" in by:
        err = (by["run_failed"].details.get("error") or "")
        act = by["run_failed"].details.get("failed_activity") or "an activity"
        if "504" in err or "timed out" in err.lower() or "timeout" in err.lower():
            cause = f"The scraper timed out: the origin site was slow or unreachable when '{act}' ran."
            action = f"Check {site}'s availability and the scraper function logs; re-run the scrape once the site responds."
        elif "403" in err or "captcha" in err.lower():
            cause = f"{site} blocked the scraper (HTTP 403)."
            action = "Check proxy/IP rotation and request rate for this site, then re-run."
        else:
            cause = f"Pipeline activity '{act}' failed: {err[:160] or 'see the activity error'}."
            action = f"Open run {run_id} in ADF, fix the failing activity, then re-run the scrape."
        impact = f"{site} was not refreshed; {out_cfg.table} still holds the previous run's data."
        confidence = "high" if err else "medium"
    elif "empty_site" in by:
        blocked = scraper_out.get("blockedReason") or (http in (401, 403, 429))
        if blocked:
            cause = f"{site} blocked the scraper ({scraper_out.get('blockedReason') or f'HTTP {http}'})."
            action = "Check proxy/IP rotation and the site's bot protection; consider lowering the request rate, then re-run."
            confidence = "high"
        else:
            cause = f"The scrape for {site} completed but parsed nothing; the listing pages may have moved or come back empty."
            action = f"Open {site} manually and compare with the scraper's start URL and selectors."
        impact = f"No {site} data from this run reached {out_cfg.table}."
    elif ns and http == 200 and not schema_changed:
        fields = ", ".join(f["field"] for f in ns.details.get("fields", []))
        cause = f"{site} most likely changed its page layout: pages still load (HTTP 200) but the {fields} selector finds nothing."
        action = f"Check the {fields} selector in the {site} scraper against the live page, then re-run."
        if rc and rc.details.get("direction") == "low":
            cause += " The low row count fits the same picture: listings without a parsable field are being dropped."
        impact = (f"{site}'s {weekday or 'latest'} data is incomplete" +
                  (f" (about {_pct(rc.details.get('drop', 0))} of rows missing)" if rc and rc.details.get("drop") else "") +
                  f"; {fields} unusable for {_pct(ns.actual or 0)} of rows. Reports reading {out_cfg.table} will under-count.")
        confidence = "high" if rc else "medium"
    elif rc and rc.details.get("direction") == "low":
        pages = scraper_out.get("pagesFetched")
        cause = (f"Partial scrape: only {pages} pages were fetched, so pagination or rate limiting likely cut the run short."
                 if pages is not None else "Partial scrape: the run stopped early or the site served fewer listings.")
        action = f"Check pagination and rate-limit handling in the {site} scraper; compare pages fetched with a normal run."
        impact = f"About {_pct(rc.details.get('drop', 0))} of {site}'s {weekday or 'latest'} data is missing from {out_cfg.table}."
    elif rc and rc.details.get("direction") == "high":
        cause = f"{site} returned far more rows than usual: a new catalogue section, a pagination loop, or duplicate pages."
        action = "Spot-check new rows for duplicates or unexpected categories before downstream jobs consume them."
        impact = f"Totals over {site} may be inflated."
    elif "stuck_trigger" in by:
        cause = "The trigger row was never picked up by ADF: the pipeline trigger may be stopped, or the integration runtime unavailable."
        action = f"Check the ADF trigger for {', '.join(ctx.cfg.pipeline.pipelines) or 'the pipeline'} and the integration runtime status."
        impact = f"{site} has not been scraped since it was requested."
        confidence = "medium"
    elif "run_too_slow" in by:
        cause = f"The {site} scrape is running much longer than usual; the site may be slow or throttling requests."
        action = "Watch the run; if it keeps climbing, check the scraper function's request latency for this site."
        impact = f"{site} data will land late."
    elif "schema_drift" in by:
        d = by["schema_drift"].details
        cause = f"{d.get('table')} changed shape: the scraper or a deployment altered the staging table."
        action = "Confirm the change with the scraper owners, update downstream queries, then accept the new schema in the portal."
        impact = ("Queries that select the removed or retyped columns will fail or misread data."
                  if d.get("removed") or d.get("retyped") else "New columns are harmless for now but untracked downstream.")
        confidence = "high"
    elif "freshness" in by:
        cause = f"No new {site} data arrived on schedule; recent runs failed, were skipped, or never triggered."
        action = f"Check the trigger table and recent pipeline runs for {site}."
        impact = f"Consumers are reading stale {site} data."
    elif "duplicates" in by:
        cause = f"The {site} scrape wrote the same keys more than once, typically a retried page or overlapping pagination."
        action = "Check the scraper's paging logic and the copy activity's write behaviour (append vs upsert)."
        impact = f"Counts and sums over {site} are inflated."
    elif "mismatch" in by:
        cause = "The trigger table and the actual output disagree."
        action = "Compare the trigger row, the pipeline run and the staging table for this site; fix whichever step reports the wrong status."
        impact = "Jobs keyed off the trigger table may proceed with missing or unexpected data."
    else:
        cause = "Several checks failed together; see the evidence."
        action = "Review the failing checks below."
        impact = "See the failing checks."
        confidence = "low"

    return Analysis(summary=summary, impact=impact, likely_cause=cause, suggested_action=action,
                    confidence=confidence, evidence=evidence, source="rules")


# ── feeds (Function Apps, Snowflake tasks / streams, scrapers writing straight into tables) ──────────────────

_PRODUCER_WORD = {
    "azure_function": "Function App",
    "snowflake_task": "Snowflake task",
    "snowflake_stream": "stream-triggered procedure",
    "manual": "manual scraper",
}


def investigate_feed(ctx: ProjectContext, incident: Incident, failing: list[Outcome]) -> Analysis:
    from .tools import ToolBox

    tools = ToolBox(ctx, incident, agent="rules")
    by = {o.check: o for o in failing}
    site = next((o.site for o in failing if o.site), None)
    feed = ctx.cfg.feed_by_name(site) if site else None
    evidence: list[dict[str, Any]] = []

    def ev(label: str, detail: str, tool: str) -> None:
        evidence.append({"label": label, "detail": detail, "tool": tool})

    summary = " ".join(o.message for o in sorted(failing, key=lambda o: o.check))

    if "schema_drift" in by and feed is None:
        d = by["schema_drift"].details
        return Analysis(
            summary=summary,
            impact="Queries and dashboards that select the removed or retyped columns will fail or misread data."
            if d.get("removed") or d.get("retyped") else "New columns are harmless for now but not yet known to downstream users.",
            likely_cause=f"{d.get('table')} changed shape: a producer deployment or a manual change altered the table.",
            suggested_action="Confirm the change with the feed owners, update downstream queries, then accept the new schema in Tables.",
            confidence="high", evidence=evidence)

    if feed is None:
        return Analysis(summary=summary, impact="See the failing checks.", likely_cause="Several checks failed together.",
                        suggested_action="Review the failing checks below.", confidence="low", evidence=evidence)

    who = feed.producer.name or _PRODUCER_WORD[feed.producer.type]
    kind = _PRODUCER_WORD[feed.producer.type]

    history = tools.call("get_trigger_rows", site=feed.name, hours=24 * 3)
    closed = [h for h in history if h["run_status"] in ("Succeeded", "Failed")]
    if closed:
        ok = sum(1 for h in closed[:8] if h["run_status"] == "Succeeded")
        ev("Recent runs", f"{ok} of the last {min(8, len(closed))} scheduled runs wrote rows", "get_trigger_rows")
    run_id = next((o.details.get("run_id") or o.run_ref for o in failing if (o.details.get("run_id") or o.run_ref)), None)
    acts: list[dict[str, Any]] = []
    if run_id and "@" in run_id:
        try:
            acts = tools.call("get_activity_runs", run_id=run_id)
        except Exception:  # noqa: BLE001 - evidence is best effort
            acts = []
    failed_act = next((a for a in acts if (a.get("status") or "").lower() == "failed"), None)
    if acts:
        ev(f"{kind} telemetry", f"{len(acts)} event(s) around the run" + (f"; latest failure: {(failed_act.get('error') or {}).get('message', '')[:200]}" if failed_act else ", none failed"),
           "get_activity_runs")
    elif feed.producer.type == "azure_function" and not feed.producer.app_insights_app_id:
        ev(f"{kind} telemetry", "Not connected yet (no Application Insights app id), so the bot can't see the function's own errors.", "config")

    cause, action, impact, confidence = "", "", "", "medium"
    when = ""
    if "missed_run" in by:
        d = by["missed_run"].details
        when = by["missed_run"].message
        streak = d.get("missed_in_a_row", 1)
        if failed_act:
            cause = f"{who} ran and failed: {(failed_act.get('error') or {}).get('message', 'see its error')[:200]}"
            confidence = "high"
        elif feed.producer.type == "azure_function":
            cause = (f"{who} did not write anything to {feed.table} after its scheduled run. The function most likely failed or timed out, "
                     f"its timer trigger is disabled, or the source API returned nothing (for example an exhausted API quota).")
        elif feed.producer.type in ("snowflake_task", "snowflake_stream"):
            cause = f"{who} produced no rows: the task may be suspended, the stream had no new data, or the notebook failed."
        else:
            cause = f"No rows arrived from {who} on schedule."
        action = {
            "azure_function": f"Open {who} → Monitor (or Application Insights) for the invocations around that time; check the timer trigger is enabled and the API quota is not exhausted, then run it once by hand.",
            "snowflake_task": f"Run SHOW TASKS / TASK_HISTORY for {feed.producer.name}; resume it if it is suspended and check the notebook's last error.",
            "snowflake_stream": f"Check {feed.producer.name} has data (SYSTEM$STREAM_HAS_DATA) and the task that calls the procedure is running.",
            "manual": "Run the scraper scripts and confirm they load into the table.",
        }[feed.producer.type]
        impact = f"{feed.name} data is missing for {'the last ' + str(streak) + ' runs' if streak > 1 else 'the latest run'}; anything reading {feed.table} is behind."
    elif "producer_failed" in by:
        d = by["producer_failed"].details
        cause = f"{who} reported a failure: {d.get('error', '')[:220]}"
        action = f"Fix the error in {who}, then re-run it; {d.get('failures_24h', 1)} failure(s) in the last 24 h."
        impact = f"{feed.name} may stop updating until {who} succeeds again."
        confidence = "high"
    elif "freshness" in by:
        cause = f"{feed.name} has not written to {feed.table} for longer than its schedule allows; {who} has stopped or is failing silently."
        action = ("Run the scraper scripts (msc_new_v1.py, maersk_new_v1.py, chrobinson_new_v1.py)." if feed.producer.type == "manual"
                  else f"Check {who} is running and its trigger/schedule is enabled.")
        impact = f"Consumers are reading stale {feed.name} data."
    elif "row_count" in by:
        rc = by["row_count"]
        low = rc.details.get("direction") == "low"
        cause = (f"{who} wrote far fewer rows than usual: the source returned less (outage, rate limit or quota), or part of the run failed."
                 if low else f"{who} wrote far more rows than usual: possible duplicate fetches or a change at the source.")
        action = f"Compare this run's rows in {feed.table} with a normal run; check the {kind}'s logs and the source API's usage/quota."
        impact = f"{feed.name} coverage for this run is {'incomplete' if low else 'inflated'}."
    elif "null_spike" in by:
        fields = ", ".join(f["field"] for f in by["null_spike"].details.get("fields", []))
        cause = f"The source changed its format: {fields} now arrive empty, so {who}'s parsing no longer matches."
        action = f"Check a recent row in {feed.table} against the source response and update {who}'s parser."
        impact = f"{fields} unusable for this run's rows."
    elif "duplicates" in by:
        cause = f"{who} wrote the same keys more than once, typically an overlapping fetch window or a retried run."
        action = "Check the merge/upsert logic and the fetch window."
        impact = f"Counts over {feed.name} are inflated."
    else:
        cause, action, impact, confidence = "Several checks failed together.", "Review the failing checks below.", "See the failing checks.", "low"

    return Analysis(summary=summary or when, impact=impact, likely_cause=cause, suggested_action=action,
                    confidence=confidence, evidence=evidence, source="rules")


# ── ADF pipelines watched directly ───────────────────────────────────────────

# Common ADF error codes / phrases → what usually causes them and what to do.
_ADF_HINTS: list[tuple[tuple[str, ...], str, str]] = [
    (("selected from empty array", "empty array", "index '0'"),
     "an earlier step (a Filter, Get Metadata or Lookup) returned nothing, so there was no item for this step to use: "
     "usually the expected input file wasn't in the source folder when the pipeline ran",
     "Check the source folder for the expected file. If 'nothing to process' is normal, guard this step with an If "
     "condition on the earlier step's output count so an empty run succeeds instead of failing."),
    (("filenotfound", "pathnotfound", "does not exist", "no such file", "blobnotfound"),
     "the source file or folder wasn't there when the pipeline ran (not delivered yet, renamed or moved)",
     "Check the source location for the expected file, then re-run the pipeline."),
    (("login failed", "authentication", "unauthorized", "forbidden", "password", "credential", "token", "403", "401"),
     "a linked service could not sign in: an expired password, secret or key, or revoked access",
     "Check the linked service's credential (Key Vault secret) and test its connection in ADF Studio, then re-run."),
    (("timeout", "timed out", "time out"),
     "a step timed out: the source or sink was slow or unreachable, or the integration runtime was busy",
     "Check the integration runtime and the source system's health; re-run once it responds."),
    (("snowflake", "sql compilation", "invalid identifier", "not authorized", "column"),
     "the Snowflake / SQL step rejected the load: a schema or permission change on the target",
     "Compare the target table with the step's mapping or query; fix the mismatch and re-run."),
    (("conversion", "parse", "format", "delimit", "column count", "schema mismatch", "invalid", "type"),
     "the data didn't match what the step expects (a format, type or column change in the source)",
     "Open the failed step's input file or query result and compare it with the mapping."),
    (("integration runtime", "selfhosted", "self-hosted", "offline"),
     "the integration runtime was unavailable",
     "Check the self-hosted integration runtime nodes are online, then re-run."),
]


def _adf_hint(text: str) -> tuple[str, str] | None:
    t = text.lower()
    for keys, cause, action in _ADF_HINTS:
        if any(k in t for k in keys):
            return cause, action
    return None


def investigate_adf(ctx: ProjectContext, incident: Incident, failing: list[Outcome]) -> Analysis:
    from ..connectors.adf import failure_path
    from .tools import ToolBox

    tools = ToolBox(ctx, incident, agent="rules")
    adf = ctx.connectors.pipeline
    by = {o.check: o for o in failing}
    site = next((o.site for o in failing if o.site), None) or incident.scope.split(":", 1)[-1]
    evidence: list[dict[str, Any]] = []

    def ev(label: str, detail: str, tool: str) -> None:
        evidence.append({"label": label, "detail": detail, "tool": tool})

    summary = " ".join(o.message for o in sorted(failing, key=lambda o: o.check))
    history = tools.call("get_trigger_rows", site=site, hours=24 * 7)
    done = [h for h in history if h["run_status"] in ("Succeeded", "Failed", "Cancelled")]
    if done:
        ok = sum(h["run_status"] == "Succeeded" for h in done[:10])
        ev("Recent runs", f"{ok} of the last {min(10, len(done))} runs of {site} succeeded", "get_trigger_rows")
    run_id = next((o.run_ref for o in failing if o.run_ref), None) or incident.run_ref
    link = adf.monitor_url(run_id) if run_id and hasattr(adf, "monitor_url") else None
    see = f" Run in ADF Studio: {link}" if link else ""

    if "run_failed" in by:
        path: list[dict[str, Any]] = []
        if run_id and hasattr(adf, "run_tree"):
            try:
                path = failure_path(adf.run_tree(site, run_id))
            except Exception as exc:  # noqa: BLE001 - evidence is best effort
                ev("Failure tree", f"Couldn't read the pipeline definition: {str(exc)[:160]}", "run_tree")
        error = (path[-1]["error"] if path else None) or by["run_failed"].details.get("error") or ""
        if path:
            steps = " → ".join(
                f"{p['type']} '{p['name']}'" + (f" (iteration {p['iteration']} of {p['of']})" if p.get("iteration") else "")
                for p in path)
            ev("Where it failed", steps, "run_tree")
        if error:
            ev("Error", error[:400], "run_tree" if path else "get_activity_runs")
        if run_id and hasattr(adf, "run_tree"):
            from .adf_explain import explain_failure

            try:
                story = explain_failure(ctx, adf, site, run_id)
                return Analysis(summary=story.summary, impact=story.impact, likely_cause=story.cause,
                                suggested_action=story.action, confidence=story.confidence,
                                evidence=[*story.evidence[:1], *evidence, *story.evidence[1:]], source="rules")
            except Exception as exc:  # noqa: BLE001 - fall back to the short explanation below
                ev("Detailed explanation", f"Couldn't build it: {str(exc)[:160]}", "run_tree")
        hint = _adf_hint(error)
        where = f"'{path[-1]['name']}'" if path else "a step"
        if hint:
            cause = f"{site} failed at {where}: {hint[0]}."
        else:
            cause = f"{site} failed at {where}" + (f": {error[:220]}" if error else ".")
        action = (hint[1] if hint else "Open the run in ADF Studio, read the failed step's error, fix it and re-run.") + see
        return Analysis(summary=summary, impact=f"The data {site} loads was not refreshed by this run; anything downstream is behind.",
                        likely_cause=cause, suggested_action=action, confidence="high" if path and error else "medium",
                        evidence=evidence, source="rules")

    if "missed_run" in by:
        d = by["missed_run"].details
        return Analysis(summary=summary, impact=f"{site} hasn't refreshed its data on schedule.",
                        likely_cause=f"Trigger {d.get('triggers')} didn't start {site}: the trigger may be stopped, its schedule "
                                     "changed, or an earlier run is still blocking it.",
                        suggested_action="In ADF Studio → Manage → Triggers, check the trigger is Started; in Monitor → Trigger runs, "
                                         "look for failed or skipped trigger runs.",
                        confidence="medium", evidence=evidence, source="rules")
    if "empty_site" in by or "row_count" in by:
        o = by.get("empty_site") or by["row_count"]
        low = "empty_site" in by or o.details.get("direction") == "low"
        if "empty_site" in by:
            impact = f"{site} copied no rows in this run."
        else:
            impact = f"{site} copied {'far fewer' if low else 'far more'} rows than usual in this run."
        cause = ("The source had little or no new data (no file delivered, an empty extract or a changed filter), "
                 "while the pipeline itself succeeded.") if low else \
            "The source delivered far more data than usual (a backfill, duplicate files or a widened filter)."
        return Analysis(summary=summary, impact=impact, likely_cause=cause,
                        suggested_action="Compare this run's copy step (rows read vs copied) with a normal run and check the source "
                                         "system for the period." + see,
                        confidence="medium", evidence=evidence, source="rules")
    if "run_too_slow" in by:
        d = by["run_too_slow"].details
        state = "is still running" if d.get("in_progress") else "finished late"
        return Analysis(summary=summary, impact=f"{site} {state}; its data lands later than usual.",
                        likely_cause="More data than usual, a slow source or sink, or the integration runtime queueing other work.",
                        suggested_action="Compare the steps' durations with a normal run to find the slow one." + see,
                        confidence="medium", evidence=evidence, source="rules")
    return Analysis(summary=summary, impact="See the failing checks.", likely_cause="Several checks failed together.",
                    suggested_action="Review the failing checks below.", confidence="low", evidence=evidence, source="rules")


def investigate_table_load(ctx: ProjectContext, incident: Incident, failing: list[Outcome]) -> Analysis:
    """One target table's load (ADF): how many rows it got compared with its own normal, and why."""
    from sqlalchemy import select

    from ..db import MetricObservation

    table = incident.scope.split(":", 1)[1]
    by = {o.check: o for o in failing}
    o = by.get("empty_site") or by.get("row_count") or by.get("rows_skipped") or failing[0]
    d = o.details or {}
    pipeline = o.site or d.get("pipeline", "")
    read, copied, skipped = d.get("rows_read"), o.actual if o.check != "rows_skipped" else None, d.get("rows_skipped") or 0
    hist = list(ctx.session.scalars(
        select(MetricObservation).where(MetricObservation.project_id == ctx.project.id, MetricObservation.output_id == f"table:{table}",
                                        MetricObservation.metric == "row_count").order_by(MetricObservation.observed_at.desc()).limit(8)))
    evidence = [{"label": "Recent loads", "tool": "query_metric",
                 "detail": "; ".join(f"{h.observed_at:%d %b %H:%M} {h.value:,.0f}" for h in hist) or "none yet"}]
    if read is not None:
        evidence.append({"label": "This load", "tool": "get_activity_runs",
                         "detail": f"{read:,.0f} rows read · {copied if copied is not None else '?'} copied · {skipped:,.0f} skipped"})
    link = ctx.connectors.pipeline.monitor_url(d["run_id"]) if d.get("run_id") and hasattr(ctx.connectors.pipeline, "monitor_url") else None
    see = f" Run in ADF Studio: {link}" if link else ""
    summary = " ".join(x.message for x in failing)
    if "rows_skipped" in by and len(by) == 1:
        return Analysis(summary=summary, impact=f"{skipped:,.0f} source rows are missing from {table}.",
                        likely_cause="The copy step skipped rows it couldn't write: values that don't fit the target column "
                                     "(type, length or format), usually a change in the source file.",
                        suggested_action="Turn on 'Log skipped rows' in the copy step's fault-tolerance settings to see exactly which "
                                         "rows and why, then fix the source or widen the target columns." + see,
                        confidence="medium", evidence=evidence, source="rules")
    low = "empty_site" in by or (by.get("row_count") and by["row_count"].details.get("direction") == "low")
    if low:
        if read is not None and copied is not None and read > 0 and copied < read * 0.9:
            cause = (f"The source still had {read:,.0f} rows, but only {copied:,.0f} were copied: rows were skipped or "
                     "filtered on the way in.")
        else:
            cause = ("The source delivered less data than usual: fewer or smaller files, an empty extract, or a filter "
                     "upstream. The pipeline itself didn't fail for this table.")
        return Analysis(summary=summary, impact=f"{table} is missing data for this load; reports reading it will show gaps.",
                        likely_cause=cause,
                        suggested_action=f"Compare this load's source files with a normal day's (count and size), and check with "
                                         f"whoever produces them. {pipeline} loads this table." + see,
                        confidence="medium", evidence=evidence, source="rules")
    return Analysis(summary=summary, impact=f"{table} received far more rows than usual; counts built on it may be inflated.",
                    likely_cause="More data than usual arrived: a backfill, the same file delivered twice, or a widened extract.",
                    suggested_action="Check the load for duplicate files or keys before the data is used." + see,
                    confidence="medium", evidence=evidence, source="rules")


def investigate_data_quality(ctx: ProjectContext, incident: Incident, failing: list[Outcome]) -> Analysis:
    """A table's data looks wrong. Lay it out like an engineer would: expected / actual / deviation, then check the
    pipeline that loads it (did the load run, how many rows did it copy, any schema change?), then the likely source."""
    from sqlalchemy import select

    from ..db import MetricObservation, Run

    table = incident.scope.split(":", 1)[1]
    by = {o.check: o for o in failing}
    pipeline = next((o.site for o in failing if o.site), "") or ""
    lines: list[str] = []
    investigation: list[str] = []
    rc = by.get("row_count")
    if rc is not None and rc.expected_low is not None:
        normal = rc.details.get("deviation")
        expected = (rc.expected_low + rc.expected_high) / 2 if rc.expected_high is not None else rc.expected_low
        lines += [f"{table} {'is missing rows' if rc.details.get('direction') == 'low' else 'has more rows than usual'} today.",
                  f"Expected: ~{expected:,.0f} rows (normal range {rc.expected_low:,.0f}–{rc.expected_high:,.0f})",
                  f"Actual: {rc.actual:,.0f} rows"]
        if normal is not None:
            lines.append(f"Deviation: {normal:+.1%}")
    lines += [o.message for o in failing if o.check != "row_count"]

    # what the pipeline that loads this table did
    load = ctx.session.scalars(select(MetricObservation).where(
        MetricObservation.project_id == ctx.project.id, MetricObservation.output_id == f"table:{table}",
        MetricObservation.metric == "row_count").order_by(MetricObservation.observed_at.desc()).limit(8)).all()
    if load:
        last = load[0]
        prev = [x.value for x in load[1:8]]
        avg = sum(prev) / len(prev) if prev else None
        investigation.append(f"Latest load by {pipeline or 'its pipeline'} copied {last.value:,.0f} rows on {last.observed_at:%a %d %b %H:%M} UTC"
                             + (f" (previous loads averaged {avg:,.0f})." if avg else "."))
        run = ctx.session.scalars(select(Run).where(Run.project_id == ctx.project.id, Run.run_id == last.run_ref)).first()
        if run is not None:
            investigation.append(f"That pipeline run {'completed successfully' if run.run_status == 'Succeeded' else 'ended ' + (run.run_status or '?')}.")
        source_low = bool(avg) and last.value < 0.7 * avg
    else:
        investigation.append(f"No Data Factory load recorded for {table} recently.")
        source_low = False
    schema_changed = "schema_drift" in by
    investigation.append("Schema change detected." if schema_changed else "No schema change detected.")
    nulls = by.get("null_spike")

    if "freshness" in by:
        cause = "New rows haven't arrived: the load that feeds this table hasn't run or hasn't written to it since."
        action = f"Check {pipeline or 'the pipeline'} ran and loaded {table}; then check the source delivered files."
    elif rc is not None and rc.details.get("direction") == "low":
        cause = ("The source delivered fewer records than usual (the load copied fewer rows too)." if source_low else
                 "The load copied a normal amount, so rows are missing after loading: a filter, a delete or a different load-date.")
        action = ("Check the upstream source / file delivery for today." if source_low else
                  f"Compare today's load with {table}'s rows by load date; look for deletes or a changed date column.")
    elif nulls is not None:
        fields = ", ".join(f["field"] for f in nulls.details.get("fields", [])[:4])
        cause = f"The source changed its format or stopped sending {fields}: the load still runs but those columns arrive empty."
        action = f"Open a sample of today's rows in {table} and the source file; update the mapping or chase the sender."
    elif "duplicates" in by:
        cause = "The same data was loaded twice (a re-run or the same file delivered again) or the source repeats records."
        action = "Check the pipeline's recent runs for re-runs of the same files; de-duplicate before the data is used."
    elif "distribution_shift" in by:
        cause = "The source's make-up changed (a new category, a missing segment, or a units/format change), not the load itself."
        action = "Compare today's values with yesterday's for the columns named above; confirm with the data owner whether it's real."
    elif "business_rule" in by or "value_range" in by or "referential_integrity" in by:
        cause = "Some of today's values break what the data should look like; usually a source-side change or a bad batch."
        action = "Look at the offending rows (the failing check names the column/rule) and confirm with the data owner."
    elif schema_changed:
        cause = "The table's columns changed (a deployment or a source change)."
        action = "Confirm the change is intended, update anything that reads the table, then accept the new schema."
    else:
        cause, action = "Several data checks failed together.", "Review the failing checks below."
    evidence = [{"label": "Investigation", "detail": " ".join(investigation), "tool": "query_metric"}]
    return Analysis(summary="\n".join(lines) + "\n\nInitial investigation:\n" + "\n".join(f"• {x}" for x in investigation),
                    impact=f"Reports and jobs that read {table} may be incomplete or wrong today.",
                    likely_cause=cause, suggested_action=action, confidence="medium", evidence=evidence, source="rules")
