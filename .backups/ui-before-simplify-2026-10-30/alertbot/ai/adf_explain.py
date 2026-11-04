"""A detailed, plain-language explanation of a failed Azure Data Factory run.

It reads the run the way an engineer would in ADF Studio, and writes it down:
  what ran          every step's outcome; what still loaded (rows, files)
  why it failed     each failed step, followed back to the step that fed it
                    (e.g. Copy ← Filter kept 0 of 1 files ← Get Metadata found only test_file.txt in folder BA_ID)
  pattern           how often this pipeline fails, the different failure kinds over time, and what triggers it
  impact            which targets weren't refreshed, and since when
  what to do        numbered steps, specific to the cause, with a link to the run
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..db import Run

_ACT_REF = re.compile(r"activity\('([^']+)'\)")
_TARGET = re.compile(r"Operation on target (.+?) failed: (.*)", re.S)
_ERR_CODE = re.compile(r"ErrorCode=(\w+)")
CONTAINERS = {"ForEach", "IfCondition", "Switch", "Until", "ExecutePipeline"}

# What common ADF error codes mean, in words a data engineer would use.
ERROR_MEANINGS = {
    "DelimitedTextMoreColumnsThanDefined": "a source file has more columns than the dataset defines (a new column upstream, or a delimiter inside a value)",
    "DelimitedTextColumnNameNotAllowNull": "a source file has an empty column header",
    "UserErrorFileNotFound": "the expected source file wasn't there",
    "UserErrorSourceBlobNotExist": "the expected source blob wasn't there",
    "SqlFailedToConnect": "the database couldn't be reached or refused the login",
    "InvalidTemplate": "an expression in the step couldn't be evaluated",
    "UserErrorInvalidColumnMappingColumnNotFound": "a mapped column doesn't exist in the source or target any more",
    "TypeConversionFailure": "a value couldn't be converted to the target column's type",
    "2200": "the copy step failed while reading or writing data",
}


def when(dt: datetime | None) -> str:
    return f"{dt:%a %d %b %H:%M} UTC" if dt else "unknown time"


def mins(s: float | None) -> str:
    if s is None:
        return "?"
    return f"{s / 60:.1f} min" if s >= 90 else f"{s:.0f} s"


def kind_of(message: str) -> str:
    """A short, stable name for a failure, so failures can be grouped over time."""
    if "empty array" in message:
        return "nothing to load (an input folder had no matching file)"
    if m := _ERR_CODE.search(message):
        code = m.group(1)
        return ERROR_MEANINGS.get(code, code)
    return re.sub(r"'[^']*'", "'…'", message.strip().splitlines()[0])[:90]


@dataclass
class Story:
    summary: str
    cause: str
    impact: str
    action: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: str = "medium"


def _first(acts: list[Any]) -> Any | None:
    return acts[-1] if acts else None


def _upstream(step: Any, by_name: dict[str, list[Any]]) -> dict[str, Any] | None:
    """Follow a failed step's expression back to the step that fed it (Filter / Lookup / Get Metadata)."""
    msg = (step.error or {}).get("message", "")
    refs = _ACT_REF.findall(msg) or _ACT_REF.findall(str(step.input))
    for ref in refs:
        src = _first(by_name.get(ref, []))
        if src is None:
            continue
        out = src.output or {}
        info: dict[str, Any] = {"step": src.name, "type": src.type}
        if src.type == "Filter":
            info.update(kept=out.get("FilteredItemsCount"), total=out.get("ItemsCount"),
                        condition=str((src.input or {}).get("condition", "")).lstrip("@"))
            feeder = _ACT_REF.findall(str((src.input or {}).get("items", "")))
            gm = _first(by_name.get(feeder[0], [])) if feeder else None
            if gm is not None and gm.type == "GetMetadata":
                info.update(folder=(gm.output or {}).get("itemName"),
                            files=[c.get("name") for c in (gm.output or {}).get("childItems") or []])
        elif src.type == "GetMetadata":
            info.update(folder=out.get("itemName"), files=[c.get("name") for c in out.get("childItems") or []])
        elif src.type == "Lookup":
            info.update(rows=out.get("count"))
        return info
    return None


def _source(step: Any) -> str | None:
    s = ((step.input or {}).get("source") or {}).get("storeSettings") or {}
    path = "/".join(x for x in (s.get("wildcardFolderPath") or s.get("folderPath"), s.get("wildcardFileName") or s.get("fileName")) if x)
    return path or None


def _sink(step: Any) -> str | None:
    t = ((step.input or {}).get("sink") or {}).get("type", "")
    return {"SnowflakeV2Sink": "Snowflake", "SnowflakeSink": "Snowflake", "AzureSqlSink": "Azure SQL",
            "DelimitedTextSink": "files", "ParquetSink": "Parquet files"}.get(t, t.removesuffix("Sink") or None)


def _walk(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for n in tree:
        out.append(n)
        out += _walk(n.get("children") or [])
        for c in n.get("called") or []:
            out += _walk(c.get("steps") or [])
    return out


def _history(ctx: Any, site: str, run_id: str) -> dict[str, Any]:
    runs = list(ctx.session.scalars(
        select(Run).where(Run.project_id == ctx.project.id, Run.site == site, Run.run_status.in_(("Succeeded", "Failed", "Cancelled")),
                          Run.requested_at >= ctx.now - timedelta(days=60)).order_by(Run.requested_at.desc())))
    failed = [r for r in runs if r.run_status != "Succeeded"]
    streak = 0
    for r in runs:
        if r.run_status == "Succeeded":
            break
        streak += 1
    last_ok = next((r for r in runs if r.run_status == "Succeeded"), None)
    kinds: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "steps": Counter(), "first": None, "last": None})
    for r in failed:
        m = _TARGET.search(r.error or "")
        target, msg = (m.group(1), m.group(2)) if m else ("?", r.error or "")
        k = kinds[kind_of(msg)]
        k["count"] += 1
        k["steps"][target] += 1
        k["first"] = r.requested_at if k["first"] is None or r.requested_at < k["first"] else k["first"]
        k["last"] = r.requested_at if k["last"] is None or r.requested_at > k["last"] else k["last"]
    triggers = Counter((r.pipeline or "").split(" · ")[0] for r in runs if r.pipeline)
    return {"runs": runs, "failed": failed, "streak": streak, "last_ok": last_ok, "kinds": dict(kinds), "triggers": triggers,
            "since": runs[-1].requested_at if runs else None, "this": next((r for r in runs if r.run_id == run_id), None)}


def explain_failure(ctx: Any, adf: Any, site: str, run_id: str) -> Story:
    acts = adf.get_activity_runs(run_id)
    by_name: dict[str, list[Any]] = defaultdict(list)
    for a in acts:
        by_name[a.name].append(a)
    try:
        nodes = {n["name"]: n for n in _walk(adf.run_tree(site, run_id))}
    except Exception:  # noqa: BLE001 - the definition is a bonus (target dataset names, steps not run)
        nodes = {}
    hist = _history(ctx, site, run_id)
    run: Run | None = hist["this"]
    link = adf.monitor_url(run_id)
    ev: list[dict[str, Any]] = []

    failed = [a for a in acts if a.status == "Failed" and a.type not in CONTAINERS
              and "inner activity failed" not in (a.error or {}).get("message", "")]
    copies_ok = [a for a in acts if a.type == "Copy" and a.status == "Succeeded"]
    not_run = [n for n in nodes.values() if n["status"] == "NotRun"]
    explained = [(f, _upstream(f, by_name)) for f in failed]
    empty = [(f, u) for f, u in explained if u and u.get("kept") == 0]
    all_empty = bool(failed) and len(empty) == len(failed)
    trigger = (run.pipeline or "") if run else ""

    # ── what happened ──
    head = (f"This run started {when(run.started_at)}" + (f", started by {trigger}" if trigger else "")
            + f", and ended Failed after {mins(run.duration_s)}." if run else f"Run {run_id} failed.")
    lines = [head]
    loads = [a for a in acts if a.type == "Copy"]
    if loads:
        lines.append(f"• {len(failed)} of {len(loads) if len(loads) >= len(failed) else len(failed)} load steps failed: "
                     + ", ".join(f"'{f.name}'" for f in failed[:6]) + ("…" if len(failed) > 6 else "") + ".")
    elif failed:
        lines.append(f"• Failed: {', '.join(repr(f.name) for f in failed[:6])}.")
    for a in copies_ok[:4]:
        o = a.output or {}
        what = f"{o.get('rowsCopied'):,} rows" if o.get("rowsCopied") is not None else f"{o.get('filesWritten') or o.get('filesRead') or '?'} files"
        lines.append(f"• '{a.name}' succeeded: copied {what}" + (f" from {o.get('filesRead')} file(s)" if o.get("filesRead") and o.get("rowsCopied") is not None else "") + ".")
    if len(copies_ok) > 4:
        lines.append(f"• …and {len(copies_ok) - 4} more copy steps succeeded.")
    if not_run:
        lines.append(f"• {len(not_run)} step(s) didn't run because an earlier step failed: {', '.join(repr(n['name']) for n in not_run[:5])}.")
    summary = "\n".join(lines)

    # ── why ──
    if all_empty:
        folders = [u.get("folder") or u["step"] for _, u in empty]
        cond = next((u.get("condition") for _, u in empty if u.get("condition")), "")
        cause_lines = [f"Nothing new to load in {len(empty)} input folder{'s' if len(empty) > 1 else ''} "
                       f"({', '.join(folders[:6])}). Each failed step takes the first file its Filter kept, "
                       f"but the Filter kept none, so the step had no file to name (\"output.value[0]\" of an empty list):"]
        for f, u in empty:
            files = u.get("files")
            found = (f"the folder only held {', '.join(files[:4])}" if files else "the folder was empty") if files is not None else ""
            cause_lines.append(f"• '{f.name}' ← '{u['step']}' kept {u.get('kept')} of {u.get('total')} item(s)"
                               + (f"; {found}" if found else "") + ".")
        if cond:
            cause_lines.append(f"The Filters keep only files matching {cond}.")
        cause_lines.append("This is a missing-input situation, not a broken connection or bad data.")
        if "BlobEvents" in trigger or "Event" in trigger:
            cause_lines.append(f"The pipeline is started by a storage event ({trigger.split(' · ')[-1]}): it runs whenever any one "
                               "file lands, then fails on the folders that are still empty.")
        cause = "\n".join(cause_lines)
        confidence = "high"
    else:
        cause_lines = []
        for f, u in explained[:5]:
            err = (f.error or {}).get("message", "").strip()
            code = (f.error or {}).get("errorCode", "")
            meaning = kind_of(err)
            src = _source(f)
            line = f"• '{f.name}' failed" + (f" reading {src}" if src else "") + f": {meaning}."
            if u and u.get("kept") == 0:
                line += f" Its input '{u['step']}' kept 0 of {u.get('total')} items."
            cause_lines.append(line)
            ev.append({"label": f"Error in '{f.name}'", "detail": (f"{code}: " if code else "") + err[:400], "tool": "get_activity_runs"})
        first = kind_of((failed[0].error or {}).get("message", "")) if failed else (run.error if run else "unknown")
        cause = (f"{site} failed because {first}." if failed else f"{site} failed: {(run.error or '')[:300] if run else ''}")
        if cause_lines:
            cause += "\n" + "\n".join(cause_lines)
        confidence = "high" if failed else "medium"

    # ── impact + pattern ──
    imp = []
    targets = [(f.name, nodes.get(f.name, {}).get("writes") or [], _sink(f)) for f in failed]
    named = [f"{', '.join(w)}" + (f" ({s})" if s else "") for _, w, s in targets if w]
    if named:
        imp.append(f"• Not refreshed by this run: {'; '.join(named[:6])}.")
    elif failed:
        imp.append(f"• Not refreshed by this run: whatever {', '.join(repr(n) for n, _, _ in targets[:4])} load.")
    loaded = [f"'{a.name}' {int((a.output or {})['rowsCopied']):,} rows" for a in copies_ok if (a.output or {}).get("rowsCopied")]
    if loaded:
        imp.append(f"• Still loaded by this run: {'; '.join(loaded[:5])}" + ("…" if len(loaded) > 5 else "") + ".")
    last_ok = hist["last_ok"]
    if last_ok:
        age = (ctx.now - (last_ok.ended_at or last_ok.requested_at)).total_seconds() / 3600
        imp.append(f"• The pipeline last fully succeeded {when(last_ok.ended_at or last_ok.requested_at)} ({age:.0f} h ago)"
                   + (f", copying {last_ok.rows_loaded:,} rows." if last_ok.rows_loaded else "."))
    n_all, n_fail = len(hist["runs"]), len(hist["failed"])
    if n_all:
        imp.append(f"• Pattern: {n_fail} of its last {n_all} runs failed (since {hist['since']:%d %b})"
                   + (f"; the last {hist['streak']} in a row." if hist["streak"] > 1 else "."))
    for k, info in sorted(hist["kinds"].items(), key=lambda kv: -kv[1]["count"])[:3]:
        steps = ", ".join(f"'{s}'" for s, _ in info["steps"].most_common(2))
        imp.append(f"   – {info['count']}× {k} (step {steps}), {info['first']:%d %b}–{info['last']:%d %b}")
    impact = "\n".join(imp) or f"The data {site} loads was not refreshed by this run."

    # ── what to do ──
    if all_empty:
        guard = empty[0][1]["step"]
        folders = ", ".join(sorted({u.get("folder") or u["step"] for _, u in empty}))
        steps = [f"Check whether new files were expected in {folders}. If they were, follow up with whoever delivers them.",
                 "If an empty folder is normal (the history suggests it often is), make the pipeline tolerate it: put each load "
                 f"step inside an If Condition such as @greater(activity('{guard}').output.FilteredItemsCount, 0), so a run with "
                 "nothing to load succeeds and only real problems alert."]
        if any("test_file" in " ".join(u.get("files") or []) for _, u in empty):
            steps.append("Remove the leftover test_file.txt from the inbound folders; it's the only thing some of them contain.")
        if "BlobEvents" in trigger:
            steps.append("Consider one event trigger per inbound folder (or a scheduled run after all files usually arrive), "
                         "so a file landing in one folder doesn't start the loads for all of them.")
    else:
        code = next((c for f in failed if (c := (f.error or {}).get("errorCode"))), "")
        msg = " ".join((f.error or {}).get("message", "") for f in failed)
        steps = []
        if "MoreColumnsThanDefined" in msg:
            steps += ["Open the newest source file and compare its header with the dataset's schema: look for a new column "
                      "or a delimiter inside a quoted value.",
                      "Either update the dataset / mapping to the new layout, or fix the file at the source; then re-run."]
        elif code == "InvalidTemplate":
            steps.append("Look at the failed step's expression in ADF Studio and the outputs of the steps it reads from.")
        else:
            steps.append("Read the failed step's error in ADF Studio (Output / Error tabs) and fix the cause.")
        steps.append("Re-run the pipeline once fixed, and check the next run succeeds.")
    # another, different problem this pipeline has had recently deserves its own step
    this_kind = kind_of((failed[0].error or {}).get("message", "")) if failed else ""
    for k, info in sorted(hist["kinds"].items(), key=lambda kv: -kv[1]["count"]):
        if k != this_kind and info["last"] and ctx.now - info["last"] < timedelta(days=14):
            step, _ = info["steps"].most_common(1)[0]
            steps.append(f"Separately, '{step}' has failed {info['count']}× since {info['first']:%d %b} because {k} "
                         f"(last on {info['last']:%d %b}). That's a different problem worth fixing too.")
            break
    steps.append(f"Open this run in ADF Studio: {link}")
    action = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))

    # ── evidence ──
    if run:
        ev.insert(0, {"label": "Run", "detail": f"{run_id} · {when(run.started_at)} · {trigger or 'manual'} · {mins(run.duration_s)}", "tool": "get_adf_run"})
    filters = [a for a in acts if a.type == "Filter"]
    if filters:
        ev.append({"label": "What each Filter found", "detail": "; ".join(
            f"'{a.name}' kept {(a.output or {}).get('FilteredItemsCount')} of {(a.output or {}).get('ItemsCount')}" for a in filters[:8]),
            "tool": "get_activity_runs"})
    for _f, u in empty[:4]:
        if u.get("files") is not None:
            ev.append({"label": f"Folder {u.get('folder') or u['step']}", "detail": "contains: " + (", ".join(u["files"][:8]) or "nothing"),
                       "tool": "get_activity_runs"})
    return Story(summary=summary, cause=cause, impact=impact, action=action, evidence=ev, confidence=confidence)
