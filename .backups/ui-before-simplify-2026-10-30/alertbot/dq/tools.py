"""The data-quality checks as LangChain tools.

They are built per table per run (bound to that run's TableRun, so concurrent runs never share results) and are
invoked by the workflow in graph.py. No model is ever given these tools, and none of them takes SQL, a table name or a
column name as input: what each one reads was fixed when the table was mapped. Each returns numbers only.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict

from ..timeutil import iso
from .service import TableRun, as_utc


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _pct(part: Any, whole: int) -> float | None:
    return round(float(part) / whole, 6) if part is not None and whole else None


def make_tools(run: TableRun, now_utc: Any, tz: Any) -> dict[str, BaseTool]:
    plan = run.plan

    def check_row_count() -> dict[str, Any]:
        return {"total_records": run.rows_total(), "records_today": run.rows_today() if plan.date_column else None}

    def check_null_percentage() -> dict[str, Any]:
        n = run.rows_today()
        nulls, empties = run.values("null"), run.values("empty")
        return {c: {"null_count": v, "null_percentage": _pct(v, n), "empty_string_count": empties.get(c)} for c, v in nulls.items()}

    def check_duplicate_count() -> dict[str, Any]:
        rows, groups = run.duplicates()
        return {"duplicate_records": rows, "duplicate_groups": groups, "records_with_empty_key": run.values("null_key").get(""),
                "duplicate_percentage": _pct(rows, run.rows_today())}

    def check_data_freshness() -> dict[str, Any]:
        at = as_utc(run.latest(), tz)
        return {"latest_record_at": iso(at), "delay_hours": round((now_utc - at).total_seconds() / 3600, 3) if at else None}

    def check_schema_changes() -> dict[str, Any]:
        return {"column_count": len(plan.columns)}  # the comparison with the accepted baseline happens in evaluation

    def check_invalid_datatypes() -> dict[str, Any]:
        n = run.rows_today()
        return {c: {"invalid_count": v, "invalid_percentage": _pct(v, n)} for c, v in run.values("invalid").items()}

    def check_business_rule_violations() -> dict[str, Any]:
        n = run.rows_today()
        return {r: {"violations": v, "violation_percentage": _pct(v, n)} for r, v in run.values("rule").items()}

    def check_referential_integrity() -> dict[str, Any]:
        return {k: {"orphan_records": v} for k, v in run.orphans().items()}

    def check_value_ranges() -> dict[str, Any]:
        n = run.rows_today()
        return {"out_of_range": {c: {"count": v, "percentage": _pct(v, n)} for c, v in run.values("out_of_range").items()},
                "negative": {c: v for c, v in run.values("negative").items()}}

    def check_missing_partitions() -> dict[str, Any]:
        recent, before = run.partitions()
        return {"days_with_rows_last_7": recent, "days_with_rows_previous_21": before}

    def check_distribution() -> dict[str, Any]:
        stats: dict[str, dict[str, Any]] = {}
        for metric in ("min", "max", "avg", "stddev"):
            for c, v in run.values(metric).items():
                stats.setdefault(c, {})[metric] = v
        return {"statistics": stats, "averages": run.values("mean"), "most_common_value_share": run.shares()}

    fns = [check_row_count, check_null_percentage, check_duplicate_count, check_data_freshness, check_schema_changes,
           check_invalid_datatypes, check_business_rule_violations, check_referential_integrity, check_value_ranges, check_distribution,
           check_missing_partitions]
    return {fn.__name__: StructuredTool.from_function(func=fn, name=fn.__name__, description=(fn.__doc__ or fn.__name__.replace("_", " ")),
                                                      args_schema=NoArgs) for fn in fns}


def select_checks(run: TableRun) -> list[str]:
    """Which tools apply to this table (from its plan)."""
    p = run.plan
    chosen = ["check_row_count", "check_null_percentage", "check_schema_changes"]
    if p.date_column:
        chosen += ["check_data_freshness", "check_missing_partitions"]
    if p.key or run.client.dialect.name == "snowflake":
        chosen.append("check_duplicate_count")
    if p.typed:
        chosen.append("check_invalid_datatypes")
    if p.rules:
        chosen.append("check_business_rule_violations")
    if p.references:
        chosen.append("check_referential_integrity")
    if p.ranges or p.numeric:
        chosen.append("check_value_ranges")
    if p.stats or p.measures or p.categories:
        chosen.append("check_distribution")
    return chosen
