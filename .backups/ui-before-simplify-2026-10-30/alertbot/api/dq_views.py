"""The data-quality report, per table: thirteen checks as cards (each with what was measured, what's normal and why it
passed or failed), the empty share of every column, rows against their normal, duplicates, values, schema, the loading
pipeline's runs, and the AI's explanation. Everything here is built from aggregates the bot already stored."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import median
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import BotState, CheckResult, Incident, MetricObservation, Project, Run, SchemaSnapshot
from ..timeutil import iso, utcnow

ACTIVE = ("open", "acknowledged")

# the thirteen checks, in the order people think about them: (key, label, what it watches, checks it reads, scopes)
CARDS: list[tuple[str, str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("row_count", "Row count", "Rows that arrived today against this table's normal for the weekday", ("row_count", "empty_site"), ("data", "table")),
    ("null_spike", "Empty values", "The share of empty values in every column against its normal", ("null_spike",), ("data",)),
    ("duplicates", "Duplicates", "Rows repeating the business key (or identical rows)", ("duplicates",), ("data",)),
    ("freshness", "Freshness", "How old the newest row is", ("freshness",), ("data",)),
    ("schema_drift", "Schema changes", "Columns added, removed or retyped since the accepted schema", ("schema_drift",), ("data",)),
    ("value_range", "Value ranges", "Negative values where there never were any, and values outside allowed ranges",
     ("value_range", "out_of_range"), ("data",)),
    ("invalid_type", "Data types", "Text columns that must read as numbers or dates", ("invalid_type",), ("data",)),
    ("distribution_shift", "Distribution", "Averages and the mix of values against their normal", ("distribution_shift",), ("data",)),
    ("referential_integrity", "Referential integrity", "Values that must exist in another table", ("referential_integrity",), ("data",)),
    ("business_rule", "Business rules", "Conditions every row should meet", ("business_rule",), ("data",)),
    ("run_too_slow", "Pipeline duration", "How long the loading pipeline ran against its normal", ("run_too_slow",), ("site",)),
    ("run_failed", "Pipeline failures and retries", "Failed runs of the loading pipeline, and re-runs after a failure",
     ("run_failed", "missed_run"), ("site",)),
    ("missing_partitions", "Missing partitions and files", "Days with no rows, and loads that copied nothing",
     ("missing_partitions", "empty_site", "rows_skipped"), ("data", "table")),
    ("source_available", "Source availability", "Whether the bot could reach the database and the pipeline its source",
     ("source_available",), ("data",)),
]
CONFIGURABLE = {"referential_integrity": "Add a reference in this table's settings (column → table.column).",
                "business_rule": "Add a rule in this table's settings, e.g. AMOUNT greater than 0.",
                "invalid_type": "Mark text columns that must be numbers or dates in this table's settings.",
                "duplicates": "Set the business key in this table's settings."}


def _history(session: Session, project_id: int, obj: str, days: int = 60) -> list[dict[str, Any]]:
    """Every check run of the table, oldest first: {ref, at, pipeline, values{(metric, field): value}}."""
    runs: dict[str, dict[str, Any]] = {}
    for o in session.scalars(select(MetricObservation).where(
            MetricObservation.project_id == project_id, MetricObservation.output_id == f"dq:{obj}",
            MetricObservation.observed_at >= utcnow() - timedelta(days=days)).order_by(MetricObservation.observed_at)):
        r = runs.setdefault(o.run_ref, {"ref": o.run_ref, "at": o.observed_at, "pipeline": o.site, "values": {}})
        r["values"][(o.metric, o.field)] = o.value
    return sorted(runs.values(), key=lambda r: r["at"])


def _normal(hist: list[dict[str, Any]], metric: str, fld: str = "") -> float | None:
    prev = [h["values"][(metric, fld)] for h in hist[:-1] if (metric, fld) in h["values"]]
    return median(prev[-28:]) if prev else None


def _latest_checks(session: Session, project_id: int, obj: str, pipeline: str | None) -> dict[str, list[CheckResult]]:
    """The most recent result of every check (per rule / reference) on the table, its loads and its pipeline."""
    scopes = [f"data:{obj}", f"table:{obj}"] + ([f"site:{pipeline}"] if pipeline else [])
    out: dict[str, dict[str, CheckResult]] = defaultdict(dict)
    for r in session.scalars(select(CheckResult).where(CheckResult.project_id == project_id, CheckResult.scope.in_(scopes),
                                                       CheckResult.status.in_(("pass", "fail")))
                             .order_by(CheckResult.created_at.desc(), CheckResult.id.desc()).limit(800)):
        sub = str((r.details or {}).get("rule") or (r.details or {}).get("reference") or "")
        out[r.check].setdefault(f"{r.scope.split(':', 1)[0]}|{sub}", r)
    return {k: list(v.values()) for k, v in out.items()}


def _pct(v: float | None, digits: int = 1) -> str:
    return "–" if v is None else f"{v * 100:.{digits}f}%"


def _n(v: float | None) -> str:
    return "–" if v is None else f"{v:,.0f}"


def _dur(m: float | None) -> str:
    if m is None:
        return "–"
    return f"{m:.1f} min" if m < 90 else f"{m / 60:.1f} h"


def _cards(obj: str, latest: dict[str, list[CheckResult]], v: dict[tuple[str, str], float], hist: list[dict[str, Any]], settings: dict[str, Any],
           pstats: dict[str, Any], conn_status: str | None, conn_error: str | None) -> list[dict[str, Any]]:
    cards = []
    for key, label, what, checks, scopes in CARDS:
        results = [r for c in checks for r in latest.get(c, []) if r.scope.split(":", 1)[0] in scopes]
        fails = [r for r in results if r.status == "fail"]
        status = "fail" if fails else "pass" if results else "waiting"
        severity = ("critical" if any(r.severity == "critical" for r in fails) else "warning") if fails else None
        value, normal = "", ""
        detail = "; ".join(r.message.replace(f"{obj}: ", "").replace(f"{obj} ", "", 1).split(": ", 1)[-1] if r.message.startswith(obj)
                           else r.message for r in fails[:3])
        if key == "row_count":
            rows = v.get(("row_count", ""))
            n = _normal(hist, "row_count")
            value = f"{_n(rows)} rows today" if rows is not None else ""
            r0 = next((r for r in results if r.check == "row_count" and r.scope.startswith("data") and r.expected_low is not None), None)
            normal = f"normal {_n(r0.expected_low)}–{_n(r0.expected_high)}" if r0 else (f"normally ~{_n(n)}" if n is not None else "learning its normal")
        elif key == "null_spike":
            rates = {f: x for (m, f), x in v.items() if m == "null_rate"}
            if rates:
                col, worst = max(rates.items(), key=lambda kv: kv[1])
                value = f"{col} {_pct(worst)} empty" if worst > 0 else "No empty values"
                nn = _normal(hist, "null_rate", col)
                normal = f"normally {_pct(nn)}" if nn is not None and worst > 0 else f"{len(rates)} columns measured"
        elif key == "duplicates":
            rate, rows = v.get(("duplicate_rate", "")), v.get(("duplicate_rows", ""))
            if rate is not None:
                value = "No duplicates" if not rows else f"{_n(rows)} duplicate rows ({_pct(rate, 2)})"
                groups = v.get(("duplicate_groups", ""))
                normal = f"{_n(groups)} repeated keys" if groups else (f"key: {', '.join(settings.get('key') or [])}" if settings.get("key") else "identical rows")
            elif not settings.get("key"):
                status, normal = "not_set", ""
        elif key == "freshness":
            h = v.get(("freshness_hours", ""))
            if h is not None:
                value = f"newest row {h:.1f} h old" if h < 48 else f"newest row {h / 24:.1f} days old"
                r0 = next((r for r in results if r.expected_high is not None), None)
                normal = f"expected within {r0.expected_high:.0f} h" if r0 else ""
        elif key == "schema_drift":
            r0 = next(iter(results), None)
            if r0 is not None and r0.status == "fail":
                d = r0.details or {}
                value = ", ".join(x for x in (f"{len(d.get('removed') or [])} removed" if d.get("removed") else "",
                                              f"{len(d.get('retyped') or [])} retyped" if d.get("retyped") else "",
                                              f"{len(d.get('added') or [])} added" if d.get("added") else "") if x) or "Changed"
            elif r0 is not None:
                value = "No change"
        elif key == "value_range":
            neg = {f: x for (m, f), x in v.items() if m == "negative_rate" and x}
            oor = {f: x for (m, f), x in v.items() if m == "out_of_range_rate" and x}
            bad = {**neg, **oor}
            value = "All values in range" if results and not bad else (f"{len(bad)} column(s) out of range" if bad else "")
            normal = f"{len(settings.get('ranges') or [])} allowed ranges set" if settings.get("ranges") else "negatives watched"
        elif key == "invalid_type":
            inv = {f: x for (m, f), x in v.items() if m == "invalid_rate"}
            if not settings.get("typed"):
                status = "not_set" if not results else status
            elif inv:
                worst = max(inv.values())
                value = "All values read correctly" if worst == 0 else f"up to {_pct(worst, 2)} badly typed"
                normal = f"{len(inv)} typed column(s)"
        elif key == "distribution_shift":
            means = [f for (m, f) in v if m in ("mean", "top_share")]
            value = ("Stable" if status == "pass" else f"{len((fails[0].details or {}).get('fields') or [])} change(s)") if results else ""
            normal = f"{len(means)} measures watched" if means else ""
        elif key == "referential_integrity":
            if not settings.get("references"):
                status = "not_set" if not results else status
            else:
                orphans = sum(1 for r in fails)
                value = "Every reference found" if not orphans else f"{orphans} reference(s) with missing keys"
                normal = f"{len(settings['references'])} reference(s)"
        elif key == "business_rule":
            if not settings.get("rules"):
                status = "not_set" if not results else status
            else:
                n_rules = len(settings["rules"])
                value = f"{n_rules - len(fails)} of {n_rules} rules pass"
                normal = "rows breaking each rule are counted"
        elif key == "run_too_slow":
            value = f"last run {_dur(pstats.get('last_duration_min'))}" if pstats.get("last_duration_min") is not None else ""
            normal = f"normally {_dur(pstats.get('normal_duration_min'))}" if pstats.get("normal_duration_min") is not None else ""
        elif key == "run_failed":
            value = f"{pstats.get('failed_7d', 0)} failed · {pstats.get('retries_7d', 0)} retried"
            normal = f"of {pstats.get('runs_7d', 0)} runs in 7 days"
            if not results and pstats.get("runs_7d"):
                status = "fail" if pstats.get("last_run_failed") else "pass"
                severity = "critical" if status == "fail" else None
        elif key == "missing_partitions":
            recent, before = v.get(("days_recent", "")), v.get(("days_before", ""))
            if recent is not None and before is not None:
                value = f"{recent:.0f} of the last 7 days have rows"
                normal = "loads daily" if before >= 19 else "not a daily table"
                if before < 19 and status == "waiting":
                    status = "pass"
        elif key == "source_available":
            if conn_status == "error":
                status, severity, value, detail = "fail", "warning", "Can't reach the database", conn_error or detail
            elif conn_status == "ok":
                value = "Database reachable"
                status = "fail" if fails else "pass"
            normal = f"last load {pstats['hours_since_last_load']:.1f} h ago" if pstats.get("hours_since_last_load") is not None else ""
        if status == "not_set":
            value, severity = "Not set up", None
        cards.append({"key": key, "label": label, "what": what, "status": status, "severity": severity, "value": value,
                      "normal": normal, "detail": detail, "hint": CONFIGURABLE.get(key) if status == "not_set" else None,
                      "at": iso(max((r.created_at for r in results), default=None)) if results else None})
    return cards


def _score(cards: list[dict[str, Any]]) -> int | None:
    """Share of the judged checks that pass, with a failing check weighing 1.5 (warning) or 3 (critical)."""
    judged = [c for c in cards if c["status"] in ("pass", "fail")]
    if not judged:
        return None
    passed = sum(c["status"] == "pass" for c in judged)
    weight = passed + sum(3 if c["severity"] == "critical" else 1.5 for c in judged if c["status"] == "fail")
    return round(100 * passed / weight)


def _pipeline_runs(session: Session, project_id: int, pipeline: str) -> list[dict[str, Any]]:
    rows = session.scalars(select(Run).where(Run.project_id == project_id, or_(Run.pipeline == pipeline, Run.site == pipeline))
                           .order_by(Run.requested_at.desc()).limit(24))
    return [{"at": iso(r.started_at or r.requested_at), "status": r.run_status or r.trigger_status, "rows": r.rows_loaded,
             "duration_min": round(r.duration_s / 60, 2) if r.duration_s else None, "run_id": r.run_id} for r in rows][::-1]


def table_view(session: Session, project: Project, obj: str, mapping: dict[str, Any] | None, conn: Any, analysis: dict[str, Any] | None,
               detail: bool) -> dict[str, Any]:
    from ..dq.graph import pipeline_stats

    hist = _history(session, project.id, obj)
    last = hist[-1] if hist else None
    v = last["values"] if last else {}
    pipeline = (mapping or {}).get("pipeline") or (last or {}).get("pipeline")
    settings = (mapping or {}).get("settings") or {}
    latest = _latest_checks(session, project.id, obj, pipeline)
    pstats = pipeline_stats(session, project.id, pipeline, utcnow()) if pipeline else {}
    cards = _cards(obj, latest, v, hist, settings, pstats, getattr(conn, "status", None), getattr(conn, "last_error", None))
    failing = [c for c in cards if c["status"] == "fail"]
    inc = session.scalars(select(Incident).where(Incident.project_id == project.id, Incident.scope == f"data:{obj}",
                                                 Incident.status.in_(ACTIVE))).first()
    ai = next((t for t in ((analysis or {}).get("tables") or []) if t.get("table") == obj), None)
    rows_today, normal_rows = v.get(("row_count", "")), _normal(hist, "row_count")
    nulls = sorted(((f, x) for (m, f), x in v.items() if m == "null_rate"), key=lambda kv: -kv[1])
    out: dict[str, Any] = {
        "table": obj, "name": obj.split(".")[-1], "schema": obj.split(".")[-2] if obj.count(".") >= 1 else "",
        "database": obj.split(".")[0] if obj.count(".") == 2 else "", "pipeline": pipeline,
        "mapping": mapping, "connection": {"name": conn.name, "kind": conn.kind, "status": conn.status} if conn is not None else None,
        "status": "fail" if failing else ("ok" if any(c["status"] == "pass" for c in cards) else "learning"),
        "severity": "critical" if any(c["severity"] == "critical" for c in failing) else ("warning" if failing else None),
        "score": _score(cards), "checked_at": iso(last["at"]) if last else None,
        "last_load_at": iso(utcnow() - timedelta(hours=pstats["hours_since_last_load"])) if pstats.get("hours_since_last_load") is not None else None,
        "rows_today": rows_today, "rows_total": v.get(("rows_total", "")), "rows_normal": normal_rows,
        "rows_delta": (rows_today / normal_rows - 1) if rows_today is not None and normal_rows else None,
        "freshness_hours": v.get(("freshness_hours", "")), "duplicate_rate": v.get(("duplicate_rate", "")),
        "worst_null": {"column": nulls[0][0], "rate": nulls[0][1]} if nulls else None,
        "cards": [{k: c[k] for k in ("key", "label", "status", "severity")} for c in cards] if not detail else cards,
        "failing": len(failing), "passing": sum(c["status"] == "pass" for c in cards),
        "incident": {"id": inc.id, "title": inc.title, "severity": inc.severity} if inc else None,
        "ai": {k: ai.get(k) for k in ("verdict", "headline")} if ai and not detail else ai,
    }
    if not detail:
        return out
    snap = session.scalars(select(SchemaSnapshot).where(SchemaSnapshot.project_id == project.id, SchemaSnapshot.output_id == f"dq:{obj}")
                           .order_by(SchemaSnapshot.captured_at.desc()).limit(1)).first()
    sensitive, required = set(settings.get("sensitive") or []), set(settings.get("required") or [])
    drift = next((r for r in latest.get("schema_drift", []) if r.status == "fail"), None)
    out.update({
        "columns": [{"name": c["name"], "type": c["type"], "null_rate": v.get(("null_rate", c["name"])), "empty_rate": v.get(("empty_rate", c["name"])),
                     "null_normal": _normal(hist, "null_rate", c["name"]), "sensitive": c["name"] in sensitive, "required": c["name"] in required,
                     "key": c["name"] in (settings.get("key") or []),
                     "invalid_rate": v.get(("invalid_rate", c["name"])), "negative_rate": v.get(("negative_rate", c["name"])),
                     "out_of_range_rate": v.get(("out_of_range_rate", c["name"])),
                     "stats": {m: v.get((m, c["name"])) for m in ("min", "max", "avg", "stddev") if (m, c["name"]) in v} or None,
                     "mean": v.get(("mean", c["name"])), "mean_normal": _normal(hist, "mean", c["name"]),
                     "top_share": v.get(("top_share", c["name"])), "top_share_normal": _normal(hist, "top_share", c["name"])}
                    for c in (snap.columns if snap else [])],
        "schema_change": {k: (drift.details or {}).get(k) for k in ("added", "removed", "retyped", "baseline_captured_at")} if drift else None,
        "profiled_rows": v.get(("profiled_rows", "")) or rows_today,
        "duplicates": {"rows": v.get(("duplicate_rows", "")), "groups": v.get(("duplicate_groups", "")), "rate": v.get(("duplicate_rate", "")),
                       "empty_key_rows": v.get(("null_key_rows", "")), "key": settings.get("key") or []},
        "partitions": {"recent": v.get(("days_recent", "")), "before": v.get(("days_before", ""))},
        "rules": [{"name": r.details.get("rule"), "status": r.status, "severity": r.severity, "message": r.message,
                   "rate": v.get(("rule_fail_rate", r.details.get("rule")))} for r in latest.get("business_rule", []) if r.details],
        "references": [{"reference": r.details.get("reference"), "status": r.status, "message": r.message,
                        "rate": v.get(("orphan_rate", r.details.get("reference")))} for r in latest.get("referential_integrity", []) if r.details],
        "history": [{"at": iso(h["at"]), "rows": h["values"].get(("row_count", "")), "total": h["values"].get(("rows_total", "")),
                     "fresh": h["values"].get(("freshness_hours", "")), "dup": h["values"].get(("duplicate_rate", ""))} for h in hist[-40:]],
        "loads": [{"at": iso(o.observed_at), "rows": o.value} for o in session.scalars(select(MetricObservation).where(
            MetricObservation.project_id == project.id, MetricObservation.output_id == f"table:{obj}", MetricObservation.metric == "row_count")
            .order_by(MetricObservation.observed_at.desc()).limit(30))][::-1],
        "pipeline_stats": pstats, "pipeline_runs": _pipeline_runs(session, project.id, pipeline) if pipeline else [],
        "analysis": {k: (analysis or {}).get(k) for k in ("summary", "model", "source", "note")} if analysis else None,
    })
    return out


def project_quality(session: Session, project: Project, tables: list[str], mappings: list[Any]) -> dict[str, Any]:
    """The summary across the project's checked tables, plus what the checks are doing right now."""
    from ..dq.service import canonical, mapping_view

    report = session.get(BotState, f"dq-report:{project.key}")
    live = session.get(BotState, f"dq-live:{project.key}")
    analysis = (report.value or {}).get("analysis") if report else None
    by_obj = {canonical(m.connection, m.schema_name, m.table_name): m for m in mappings}
    objs = sorted(set(tables) | set(by_obj))
    cards = [table_view(session, project, o, mapping_view(by_obj[o]) if o in by_obj else None,
                        by_obj[o].connection if o in by_obj else None, analysis, detail=False) for o in objs]
    live_v = live.value if live else None
    if live_v and live_v.get("state") != "idle" and live.updated_at and utcnow() - live.updated_at > timedelta(minutes=20):
        live_v = {"state": "idle"}  # a check that never reported back
    return {"tables": cards, "live": live_v, "report_at": (report.value or {}).get("at") if report else None,
            "analysis": {k: (analysis or {}).get(k) for k in ("summary", "model", "source", "note")} if analysis else None}


def table_detail(session: Session, project: Project, obj: str, mappings: list[Any]) -> dict[str, Any] | None:
    from ..dq.service import canonical, mapping_view

    m = next((m for m in mappings if canonical(m.connection, m.schema_name, m.table_name) == obj), None)
    known = m is not None or session.scalar(select(MetricObservation.id).where(
        MetricObservation.project_id == project.id, MetricObservation.output_id == f"dq:{obj}").limit(1)) is not None
    if not known:
        return None
    report = session.get(BotState, f"dq-report:{project.key}")
    analysis = (report.value or {}).get("analysis") if report else None
    out = table_view(session, project, obj, mapping_view(m) if m else None, m.connection if m else None, analysis, detail=True)
    out["report_at"] = (report.value or {}).get("at") if report else None
    return out
