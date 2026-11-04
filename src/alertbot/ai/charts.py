"""Words → a chart, in plain English. The model reads the request and answers with a ChartSpec from the bot's
catalogue (or a question back); its answer is normalised (synonyms, near-misses) and validated. When the model can't
or won't produce one, a keyword reader understands everyday phrasing on its own, so the chat works even without AI.
The model never writes a query and never sees any data: only the catalogue, the project's names and the conversation."""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from ..engine.charts import CATALOGUE, ChartSpec, catalogue_text
from .llm import AIUnavailable, client, parse_json

SYSTEM = """You turn requests for charts, written in everyday English, into a chart definition for the Alert Bot portal
(a data-pipeline monitoring app). You can only chart these metrics, computed by the bot from its own records:
{catalogue}

Answer with JSON only: {{"reply": "one friendly sentence", "spec": <chart definition, or null only if you must ask>}}

A chart definition:
{{"title": "short title", "viz": "stat" | "bar" | "hbar" | "line" | "area" | "pie" | "donut" | "gauge" | "table",
  "metric": "<a metric key above>", "period_days": 1-365,
  "group_by": "none" | "day" | "week" | "month" | "pipeline" | "table" | "status" | "check" | "severity",
  "filters": {{"pipeline": null, "table": null, "column": null, "status": null, "severity": null}}}}

How people talk, and what it means:
- "card", "number", "how many", "total", "count" → viz "stat". "bar", "bars", "column" → "bar". "horizontal bars", "ranking" → "hbar".
  "line", "trend", "over time" → "line". "area" → "area". "pie" → "pie". "donut"/"doughnut"/"ring" → "donut".
  "gauge", "meter", "dial" → "gauge" (only for % metrics). "table", "list", "breakdown" → "table".
- "each pipeline", "per pipeline", "by pipeline", "for every pipeline", "all pipelines" → group_by "pipeline".
  "per day"/"daily" → "day", "weekly" → "week", "monthly" → "month". Pie/donut need parts of a whole: group by pipeline, status…
- "run duration", "how long runs take", "run time", "slow" → avg_duration_min ("longest" → max_duration_min).
  "failures", "failed runs", "errors" → failed_runs. "runs", "executions" → runs. "success rate", "reliability" → success_rate.
  "rows", "records loaded", "data loaded", "volume" → rows_loaded. "incidents", "problems" → incidents_opened.
- "last month"/"this month" = 30 days, "last week"/"this week" = 7, "today" = 1, "this quarter" = 90, "this year" = 365.
  Default 30 days.
- Prefer making a reasonable chart over asking. Ask ONE short question (spec null) only if you truly can't tell what to show.
- If it asks for something not in the catalogue (values inside tables: revenue, customers, sales…), set spec to null and say
  the bot charts its monitoring records only (never the contents of tables), suggesting the closest chart it can make.
- You ONLY make charts. Anything else (small talk, questions, commands, jokes, help with other things, acknowledging or
  resolving incidents, writing, coding) → {{"reply": "", "spec": null, "off_topic": true}}. Don't answer it.

Examples:
"show me all the run duration a bar chart for each pipeline" →
{{"reply": "Here's the average run duration of each pipeline.", "spec": {{"title": "Average run duration by pipeline", "viz": "bar",
  "metric": "avg_duration_min", "period_days": 30, "group_by": "pipeline", "filters": {{}}}}}}
"total runs this month as a pie chart" →
{{"reply": "Here's how this month's runs split across pipelines.", "spec": {{"title": "Runs this month by pipeline", "viz": "pie",
  "metric": "runs", "period_days": 30, "group_by": "pipeline", "filters": {{}}}}}}
"how many runs failed last week" →
{{"reply": "Here's the number of failed runs in the last 7 days.", "spec": {{"title": "Failed runs, last 7 days", "viz": "stat",
  "metric": "failed_runs", "period_days": 7, "group_by": "none", "filters": {{}}}}}}"""

VIZ_WORDS = {"stat": "stat", "card": "stat", "number": "stat", "kpi": "stat", "count": "stat", "bar": "bar", "bars": "bar", "column": "bar",
             "columns": "bar", "bar chart": "bar", "vertical bar": "bar", "hbar": "hbar", "horizontal bar": "hbar", "horizontal bars": "hbar",
             "line": "line", "line chart": "line", "trend": "line", "area": "area", "area chart": "area", "pie": "pie", "pie chart": "pie",
             "donut": "donut", "doughnut": "donut", "ring": "donut", "gauge": "gauge", "meter": "gauge", "dial": "gauge", "table": "table",
             "list": "table"}
GROUP_WORDS = {"pipelines": "pipeline", "per pipeline": "pipeline", "each pipeline": "pipeline", "tables": "table", "daily": "day",
               "days": "day", "weekly": "week", "weeks": "week", "monthly": "month", "months": "month", "statuses": "status",
               "checks": "check", "severities": "severity", "": "none", "total": "none", "null": "none"}
METRIC_WORDS = {"duration": "avg_duration_min", "run_duration": "avg_duration_min", "avg_duration": "avg_duration_min",
                "average_duration": "avg_duration_min", "duration_min": "avg_duration_min", "max_duration": "max_duration_min",
                "failures": "failed_runs", "failed": "failed_runs", "failed_run": "failed_runs", "total_runs": "runs", "run_count": "runs",
                "run": "runs", "successful_runs": "succeeded_runs", "rows": "rows_loaded", "row_count": "rows_loaded", "incidents": "incidents_opened",
                "alerts": "alerts_sent", "failed_checks": "checks_failed", "freshness": "freshness_h", "null_rate": "empty_pct"}


def normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Forgive the model's near-misses: synonyms, odd casing, extra keys, a period given in other words."""
    spec = dict(raw)
    viz = str(spec.get("viz") or "").strip().lower()
    spec["viz"] = VIZ_WORDS.get(viz, VIZ_WORDS.get(viz.replace("_", " "), viz))
    metric = str(spec.get("metric") or "").strip().lower().replace(" ", "_")
    spec["metric"] = metric if metric in CATALOGUE else METRIC_WORDS.get(metric, metric)
    group = str(spec.get("group_by") or "none").strip().lower()
    spec["group_by"] = GROUP_WORDS.get(group, group.removeprefix("by_").removeprefix("per_"))
    days = spec.get("period_days") or spec.get("days") or spec.get("period") or 30
    try:
        spec["period_days"] = max(1, min(365, int(float(days))))
    except (TypeError, ValueError):
        spec["period_days"] = 30
    f = spec.get("filters") if isinstance(spec.get("filters"), dict) else {}
    spec["filters"] = {k: (v if v not in ("", "all", "any") else None) for k, v in f.items() if k in ("pipeline", "table", "column", "status", "severity")}
    if spec["filters"].get("status") not in (None, "succeeded", "failed"):
        spec["filters"]["status"] = "failed" if "fail" in str(spec["filters"]["status"]).lower() else "succeeded"
    if spec["filters"].get("severity") not in (None, "warning", "critical"):
        spec["filters"]["severity"] = None
    spec["title"] = str(spec.get("title") or CATALOGUE.get(spec["metric"], CATALOGUE["runs"]).label)[:90]
    return {k: spec[k] for k in ("title", "viz", "metric", "period_days", "group_by", "filters")}


# ── the keyword reader: understands everyday phrasing without a model ──

_METRICS = [
    (r"success\s*rate|reliab|succe\w* (?:%|percent)", "success_rate"),
    (r"(?:longest|slowest|max(?:imum)?)\s+(?:run|duration)", "max_duration_min"),
    (r"durat|how long|run\s*time|runtime|took|slow|minutes", "avg_duration_min"),
    (r"time to (?:resolve|fix)|mttr|resolution time", "time_to_resolve_h"),
    (r"open incident|incidents? (?:open\b|right now|currently)", "open_incidents"),
    (r"resolved|fixed incident", "incidents_resolved"),
    (r"incident|problem|issue", "incidents_opened"),
    (r"(?:failed|failing)\s+checks?|checks?\s+(?:failed|failing)", "checks_failed"),
    (r"fail|error|broke", "failed_runs"),
    (r"succe(?:ss|ed)", "succeeded_runs"),
    (r"rows?|records?|volume|data loaded|loaded", "rows_loaded"),
    (r"fresh|stale|data age|how old", "freshness_h"),
    (r"alert|notification", "alerts_sent"),
    (r"runs?|execution", "runs"),
]
_VIZ = [(r"doughnut|donut|ring", "donut"), (r"\bpie", "pie"), (r"gauge|meter|dial", "gauge"), (r"area", "area"),
        (r"horizontal", "hbar"), (r"\bline|trend|over time", "line"), (r"\bbar|column|histogram", "bar"),
        (r"table|list|breakdown", "table"), (r"card|number|how many|total|count|kpi|single", "stat")]
_GROUP = [(r"(?:each|per|by|every|all|across)\s+pipelines?", "pipeline"), (r"(?:each|per|by|every)\s+tables?", "table"),
          (r"(?:by|per)\s+status", "status"), (r"(?:by|per)\s+severity", "severity"), (r"(?:by|per)\s+check", "check"),
          (r"per day|daily|each day|by day", "day"), (r"per week|weekly|each week|by week", "week"),
          (r"per month|monthly|each month|by month", "month")]


def _period(t: str) -> int:
    m = re.search(r"(?:last|past)\s+(\d{1,3})\s*(day|week|month)", t)
    if m:
        return min(365, int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)])
    for pat, days in ((r"today|24 ?h", 1), (r"(?:this|last|past) week", 7), (r"(?:this|last|past) quarter|3 months", 90),
                      (r"(?:this|last|past) year|12 months", 365), (r"(?:this|last|past) month|30 days", 30)):
        if re.search(pat, t):
            return days
    return 30


def guess(text: str, names: dict[str, list[str]]) -> ChartSpec | None:
    """A chart from everyday words, without a model; None if no metric is recognisable."""
    t = text.lower()
    metric = next((m for pat, m in _METRICS if re.search(pat, t)), None)
    if metric is None:
        return None
    viz = next((v for pat, v in _VIZ if re.search(pat, t)), "stat")
    group = next((g for pat, g in _GROUP if re.search(pat, t)), "none")
    pipeline = next((p for p in sorted(names.get("pipelines", []), key=len, reverse=True) if p.lower() in t), None)
    table = next((x for x in names.get("tables", []) if x.split(".")[-1].lower() in t), None)
    m = CATALOGUE[metric]
    if group != "none" and group not in m.groups:
        group = "none"
    period = _period(t)
    when = "today" if period == 1 else f"last {period} days"
    title = f"{m.label}{' by ' + group if group in ('pipeline', 'table', 'status', 'severity', 'check') else ''}{' · ' + pipeline if pipeline else ''}, {when}"
    try:
        return ChartSpec.model_validate({"title": title[:90], "viz": viz, "metric": metric, "period_days": period, "group_by": group,
                                         "filters": {"pipeline": pipeline, "table": table if metric in ("rows_loaded", "checks_failed",
                                                                                                       "table_rows", "freshness_h") else None}})
    except ValidationError:
        return None


# The chart assistant only makes charts: anything else gets this, without spending a model call on it.
OFF_TOPIC = ("I can't do that, I'm only good at making charts. 📊 Want to make one now? Try “failed runs per day this month as bars” "
             "or “run duration by pipeline as a pie”.")
YES = re.compile(r"^(yes|yeah|yep|sure|ok(ay)?|go on|please|let'?s( do it)?|why not|do it|y)\b[!. ]*$", re.I)
CHART_WORDS = re.compile(
    r"chart|graph|plot|visuali[sz]|diagram|\bviz\b|pie|donut|doughnut|\bring\b|\bbars?\b|column|histogram|\blines?\b|trend|area|gauge|meter|"
    r"dial|\btable\b|\bcard\b|\bkpi\b|stat\b|over time|per (day|week|month|pipeline|table)|by (day|week|month|pipeline|table|status|severity|check)|"
    r"breakdown|split|compare|how many|count|total|number of|average|avg|mean|median|rate|percent|%|share|"
    r"\bruns?\b|fail|success|duration|slow|rows?|records|volume|incident|alert|check|freshness|latency|\bshow\b|\bdraw\b|"
    r"\bmake (it|me|one)\b|instead|switch|change (it|to)|as (a|an) |last (day|week|month|year|quarter)|this (week|month|year|quarter)|today", re.I)


STRONG = re.compile(r"chart|graph|plot|visuali[sz]|diagram|\bviz\b|\bpie\b|donut|doughnut|\bbars?\b|column|histogram|line chart|"
                    r"\btrend|area chart|gauge|\bkpi\b|breakdown|over time|per (day|week|month|pipeline|table)|"
                    r"by (day|week|month|pipeline|table|status|severity|check)", re.I)
COMMAND = re.compile(r"\b(ack|acknowledge|resolve|close|mute|unmute|silence|snooze|comment|send|notify|email|refresh|sync|re-?run|"
                     r"restart|trigger|delete|remove|fix|open|go to|take me|navigate|log ?in|sign|deploy|write|code|script|translate)\b", re.I)
QUESTION = re.compile(r"^\s*(why|who|when did|where|explain|tell me|what is|what's|what are|is there|are there|can you|could you|"
                      r"help|hi|hello|hey|thanks|joke)\b", re.I)
QUANTITY = re.compile(r"how many|how much|count|total|number of|average|rate", re.I)


def chart_related(conversation: list[dict[str, str]]) -> bool:
    """Is the newest message about a chart (or a follow-up to one)? Small talk, commands and other questions aren't."""
    last = next((m["content"] for m in reversed(conversation) if m["role"] == "user"), "").strip()
    if not last:
        return False
    if STRONG.search(last):
        return True
    if COMMAND.search(last) or (QUESTION.search(last) and not QUANTITY.search(last)):
        return False
    return bool(CHART_WORDS.search(last)) or bool(_PERIOD_ONLY.match(last))


_PERIOD_ONLY = re.compile(r"^(for )?(the )?(last|past|this)\s+\w+$", re.I)


def understand(conversation: list[dict[str, str]], names: dict[str, list[str]]) -> tuple[str, ChartSpec | None, str | None]:
    """(reply for the person, a valid spec or None, the model's name or "rules")."""
    last = next((m["content"] for m in reversed(conversation) if m["role"] == "user"), "")
    if YES.match(last.strip()):
        return ("Great! Tell me what to show and how, in your own words. For example: “success rate by pipeline as a donut”, "
                "“rows loaded per day this week”, or “how many incidents this month”."), None, "rules"
    if not chart_related(conversation):
        return OFF_TOPIC, None, "rules"
    context = (f"This project's pipelines: {', '.join(names['pipelines'][:80]) or 'none yet'}.\n"
               f"Its tables: {', '.join(names['tables'][:80]) or 'none yet'}.")
    messages = [{"role": "system", "content": SYSTEM.format(catalogue=catalogue_text())}, {"role": "system", "content": context}]
    messages += [{"role": m["role"], "content": m["content"]} for m in conversation[-10:]]
    reply, model = "", None
    try:
        ai = client()
        for attempt in range(2):
            res = ai.chat(messages, max_tokens=900, json_mode=True, temperature=0.1)
            model = res.model
            try:
                data = parse_json(res.text)
            except ValueError:
                data = {}
            if isinstance(data, dict) and data.get("off_topic"):
                return OFF_TOPIC, None, model
            reply = str(data.get("reply") or "").strip()[:500] if isinstance(data, dict) else ""
            raw = data.get("spec") if isinstance(data, dict) else None
            if not isinstance(raw, dict):
                break
            try:
                return reply or "Here's your chart.", ChartSpec.model_validate(normalise(raw)), model
            except ValidationError as exc:
                if attempt:
                    break
                messages += [{"role": "assistant", "content": res.text[:2000]},
                             {"role": "user", "content": f"That chart definition is invalid: {exc.errors()[0]['msg']}. Answer again with a valid one."}]
    except AIUnavailable:
        pass
    # no usable chart from the model (a question, a refusal, nothing, or no model): read the words ourselves, unless they ask
    # about business data inside tables, which the bot never charts
    if not BUSINESS.search(last):
        spec = guess(last, names)
        if spec is not None:
            return f"Here's {spec.title[0].lower() + spec.title[1:]}.", spec, model or "rules"
    return reply or ("I can chart the bot's monitoring records: runs, failed runs, success rate, run duration, rows loaded, incidents, "
                     "failed checks and alerts, as a card, bars, a line, an area, a pie, a donut, a gauge or a table. "
                     "What would you like to see?"), None, model


BUSINESS = re.compile(r"revenue|sales|customer|price|profit|country|order value|salary|amount spent|margin", re.I)
