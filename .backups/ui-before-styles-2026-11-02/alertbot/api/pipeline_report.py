"""Pipeline report: the team's daily pipeline summary, built from what the bot watches, and more.

    GET /api/pipeline-report?days=7

Like the report the team already reads (runs per day per pipeline, data volume per day, "today so far" against the
usual), plus what that report can't tell: whether a pipeline is overdue for its next run, today's pace against the
same time on earlier days, success rate against the period before, runtime against normal, the errors that keep
coming back (and whether there's a known fix), and when in the week pipelines run and fail.

All days are whole local days in the bot's timezone, ending today. Pure reads: nothing is changed.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.fixes import context_of, signature_of
from ..config import load_projects
from ..db import Incident, MetricObservation, Project, Run, UserProject
from ..engine.adf import TABLE_PREFIX
from ..settings import get_settings
from ..timeutil import iso, utcnow
from . import views
from .auth import SessionDep, UserDep

router = APIRouter()

OK, FAILED, RUNNING = ("Succeeded",), ("Failed", "Cancelled", "TimedOut"), ("InProgress", "Queued")
HISTORY = 7  # earlier days that "typical" is worked out from
ACTIVE = ("open", "acknowledged")


def _short(error: str | None, n: int = 220) -> str:
    first = (error or "").strip().splitlines()[0] if error else ""
    first = re.sub(r"\s+", " ", first)
    return first if len(first) <= n else first[: n - 1] + "…"


def _typical(by_day: dict[Any, list[float]], days: list[Any], cutoff: time | None) -> tuple[float | None, float | None]:
    """The average per day over `days` (all of the day, and up to `cutoff` in it): what "usual" looks like by now."""
    if not days:
        return None, None
    full = [sum(v for _, v in by_day.get(d, [])) for d in days]  # type: ignore[misc]
    upto = [sum(v for t, v in by_day.get(d, []) if cutoff is None or t <= cutoff) for d in days]  # type: ignore[misc]
    return statistics.fmean(upto), statistics.fmean(full)


def build(session: Session, projects: list[tuple[Project, str]], days: int, now: datetime | None = None,
          user_id: int | None = None) -> dict[str, Any]:
    now = now or utcnow()
    tz = get_settings().tz
    local = lambda dt: dt.replace(tzinfo=UTC).astimezone(tz)  # noqa: E731
    today = local(now).date()
    cutoff = local(now).time()
    calendar = [today - timedelta(days=days - 1 - i) for i in range(days)]
    first_day = calendar[0]
    start = datetime.combine(first_day, time.min, tzinfo=tz).astimezone(UTC).replace(tzinfo=None)
    prev_start = start - timedelta(days=days)
    hist_start = datetime.combine(today - timedelta(days=HISTORY), time.min, tzinfo=tz).astimezone(UTC).replace(tzinfo=None)
    load_from = min(prev_start, hist_start)
    earlier = [today - timedelta(days=i) for i in range(HISTORY, 0, -1)]
    # the run map: every pipeline across the period in slots of 1 h (up to a week), 2 h (two weeks) or 4 h, so it's
    # never more than ~180 slots wide
    slot_h = 1 if days <= 7 else 2 if days <= 14 else 4
    slots = days * 24 // slot_h
    start_local = datetime.combine(first_day, time.min, tzinfo=tz)
    slot_of = lambda lt: int((lt - start_local).total_seconds() // (slot_h * 3600))  # noqa: E731
    cfgs = load_projects()

    pipelines: list[dict[str, Any]] = []
    volumes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    heat = [[{"runs": 0, "failed": 0} for _ in range(24)] for _ in range(7)]
    inc_opened = inc_resolved = 0
    mttr: list[float] = []
    open_incidents: list[dict[str, Any]] = []
    quiet = 0

    for project, _role in projects:
        cfg = cfgs.get(project.key)
        runs = list(session.scalars(select(Run).where(Run.project_id == project.id, Run.requested_at >= load_from).order_by(Run.requested_at)))
        incidents = list(session.scalars(select(Incident).where(Incident.project_id == project.id,
                                                                (Incident.opened_at >= prev_start) | Incident.status.in_(ACTIVE))))
        open_by_site = {i.scope.removeprefix("site:"): i for i in incidents if i.status in ACTIVE and i.scope.startswith("site:")}
        closed_by_site: dict[str, Incident] = {}
        for i in sorted((i for i in incidents if i.status == "resolved" and i.scope.startswith("site:") and i.resolved_at), key=lambda i: i.resolved_at,
                        reverse=True):
            closed_by_site.setdefault(i.scope.removeprefix("site:"), i)
        inc_by_run = {i.run_ref: i for i in incidents if i.run_ref}
        for i in incidents:
            if i.opened_at >= start:
                inc_opened += 1
            if i.status == "resolved" and i.resolved_at and i.resolved_at >= start:
                inc_resolved += 1
                if i.resolved_at >= i.opened_at:
                    mttr.append((i.resolved_at - i.opened_at).total_seconds() / 3600)
            if i.status in ACTIVE:
                open_incidents.append({"id": i.id, "project": project.key, "title": i.title, "severity": i.severity, "status": i.status,
                                       "opened_at": iso(i.opened_at)})
        norms = views._norms(session, project, cfg) if cfg is not None and cfg.kind == "adf" else None
        try:
            from ..engine.adf import inventory_of
            inventory = {x["name"]: x for x in inventory_of(session, project.key).get("items", [])}
        except Exception:  # noqa: BLE001 - names are a bonus
            inventory = {}

        # rows loaded, per table and per pipeline, by local day
        obs = session.execute(select(MetricObservation.output_id, MetricObservation.site, MetricObservation.observed_at, MetricObservation.value)
                              .where(MetricObservation.project_id == project.id, MetricObservation.metric == "row_count",
                                     MetricObservation.output_id.like(f"{TABLE_PREFIX}%"), MetricObservation.observed_at >= load_from)).all()
        rows_by_site: dict[str, dict[Any, list[tuple[time, float]]]] = defaultdict(lambda: defaultdict(list))
        tables: dict[tuple[str, str], dict[Any, list[tuple[time, float]]]] = defaultdict(lambda: defaultdict(list))
        last_load: dict[tuple[str, str], tuple[datetime, float]] = {}
        for output_id, site, at, value in obs:
            lt = local(at)
            v = float(value or 0)
            tables[(output_id, site)][lt.date()].append((lt.time(), v))
            rows_by_site[site][lt.date()].append((lt.time(), v))
            if (output_id, site) not in last_load or at > last_load[(output_id, site)][0]:
                last_load[(output_id, site)] = (at, v)

        by_site: dict[str, list[Run]] = defaultdict(list)
        for r in runs:
            by_site[r.site].append(r)
        names = set(by_site)
        quiet += sum(1 for n, i in inventory.items() if n not in names and i.get("state", "Enabled") != "Disabled")

        for site, rs in by_site.items():
            at_of = lambda r: r.started_at or r.requested_at  # noqa: E731
            cur = [r for r in rs if r.requested_at >= start]
            prev = [r for r in rs if prev_start <= r.requested_at < start]
            if not cur and not any(r.requested_at >= hist_start for r in rs):
                continue
            daily = {d: {"date": d.isoformat(), "ok": 0, "failed": 0, "other": 0, "rows": 0.0} for d in calendar}
            ok_times: dict[Any, list[tuple[time, float]]] = defaultdict(list)
            active_days = {local(at_of(r)).date() for r in rs}
            for r in rs:
                lt = local(at_of(r))
                kind = "ok" if r.run_status in OK else "failed" if r.run_status in FAILED else "other"
                if r.run_status in OK:
                    ok_times[lt.date()].append((lt.time(), 1.0))
                if lt.date() in daily:
                    daily[lt.date()][kind] += 1
                    heat[lt.weekday()][lt.hour]["runs"] += 1
                    if kind == "failed":
                        heat[lt.weekday()][lt.hour]["failed"] += 1
                if kind == "failed" and r.requested_at >= start:
                    failures.append({"run": r, "project": project, "site": site, "incident": inc_by_run.get(r.run_id)})
            for d, pts in rows_by_site.get(site, {}).items():
                if d in daily:
                    daily[d]["rows"] += sum(v for _, v in pts)
            grid = _grid(rs, rows_by_site.get(site, {}), site, start, slots, slot_of, local, tz)
            marks = [{"slot": slot_of(local(i.opened_at)), "id": i.id, "title": i.title, "severity": i.severity, "status": i.status}
                     for i in incidents if i.scope == f"site:{site}" and i.opened_at >= start]
            seen_days = [d for d in earlier if d in active_days]
            typ_now, typ_day = _typical(ok_times, seen_days, cutoff)
            ok_today = daily[today]["ok"]
            done = [r for r in cur if r.run_status in OK + FAILED]
            done_prev = [r for r in prev if r.run_status in OK + FAILED]
            rate = sum(r.run_status in OK for r in done) / len(done) if done else None
            rate_prev = sum(r.run_status in OK for r in done_prev) / len(done_prev) if done_prev else None
            last = rs[-1]
            # cadence: the usual gap between runs; overdue when the next one is clearly late
            starts = [at_of(r) for r in rs if r.requested_at >= hist_start]
            gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:], strict=False) if (b - a).total_seconds() > 30]
            interval = statistics.median(gaps) if len(gaps) >= 3 else None
            since_last = (now - at_of(last)).total_seconds()
            enabled = inventory.get(site, {}).get("state", "Enabled") != "Disabled"
            overdue = bool(interval and enabled and last.run_status not in RUNNING and since_last > max(interval * 1.5, interval + 1800))
            durations = sorted(r.duration_s for r in cur if r.duration_s and r.run_status in OK)
            inc = open_by_site.get(site)
            failed_last = last.run_status in FAILED
            handled = views.handled_by(inc, closed_by_site.get(site), last if failed_last else None)
            state = ("running" if last.run_status in RUNNING else "handled" if handled else "failing" if inc is not None or failed_last
                     else "late" if overdue else "unstable" if any(r.run_status in FAILED for r in cur) else "healthy")
            pipelines.append({
                "key": f"{project.key}/{site}", "project": project.key, "project_name": project.display_name, "name": site,
                "trigger": (last.pipeline or "").split(" · ")[-1] or None, "state": state, "enabled": enabled,
                "daily": list(daily.values()),
                "today": {"ok": ok_today, "failed": daily[today]["failed"], "rows": daily[today]["rows"],
                          "typical_by_now": round(typ_now, 2) if typ_now is not None else None,
                          "typical_day": round(typ_day, 2) if typ_day is not None else None,
                          "pace": round(ok_today / typ_now, 3) if typ_now else None},
                "last": {"at": iso(at_of(last)), "status": last.run_status, "duration_s": last.duration_s, "run_id": last.run_id,
                         "error": _short(last.error) if failed_last else None, "raw_error": last.error if failed_last else None},
                "runtime": norms.compare(site, last.duration_s, status=last.run_status, started_at=last.started_at) if norms else None,
                "duration": {"p50": round(statistics.median(durations), 1) if durations else None,
                             "p95": round(durations[min(len(durations) - 1, int(len(durations) * 0.95))], 1) if durations else None},
                "cadence": {"interval_s": round(interval) if interval else None, "since_last_s": round(since_last),
                            "next_at": iso(at_of(last) + timedelta(seconds=interval)) if interval else None, "overdue": overdue},
                "runs": len(cur), "ok": sum(r.run_status in OK for r in cur), "failed": sum(r.run_status in FAILED for r in cur),
                "ok_prev": sum(r.run_status in OK for r in done_prev), "done_prev": len(done_prev),
                "success_rate": round(rate, 4) if rate is not None else None, "success_rate_prev": round(rate_prev, 4) if rate_prev is not None else None,
                "incident": {"id": inc.id, "title": inc.title, "severity": inc.severity, "status": inc.status} if inc else None,
                "handled": handled,
                "grid": grid, "incidents": [m for m in marks if 0 <= m["slot"] < slots],
                "snowflake": None,
            })

        for (output_id, site), per_day in tables.items():
            table = output_id.removeprefix(TABLE_PREFIX)
            cur_days = {d: sum(v for _, v in per_day.get(d, [])) for d in calendar}
            if not any(cur_days.values()) and not any(per_day.get(d) for d in earlier):
                continue
            seen_days = [d for d in earlier if per_day.get(d)]
            typ_now, typ_day = _typical(per_day, seen_days, cutoff)
            today_rows = cur_days[today]
            at, rows = last_load[(output_id, site)]
            pct = (today_rows / typ_now - 1) if typ_now else None
            # small tables swing a lot: only call it out when the difference is worth a look
            verdict = (None if typ_now is None else "none" if today_rows == 0 and typ_now >= 5
                       else "low" if pct is not None and pct < -0.5 and typ_now - today_rows >= 50
                       else "high" if pct is not None and pct > 1.0 and today_rows - typ_now >= max(100.0, typ_now) else "normal")
            volumes.append({
                "key": f"{project.key}/{output_id}/{site}", "project": project.key, "project_name": project.display_name,
                "table": table, "short": table.split(".")[-1], "pipeline": site,
                "daily": [{"date": d.isoformat(), "rows": v} for d, v in cur_days.items()],
                "today": today_rows, "typical_by_now": round(typ_now, 1) if typ_now is not None else None,
                "typical_day": round(typ_day, 1) if typ_day is not None else None,
                "avg_7d": round(statistics.fmean([sum(v for _, v in per_day.get(d, [])) for d in seen_days]), 1) if seen_days else None,
                "pct_vs_typical": round(pct, 3) if pct is not None else None, "verdict": verdict,
                "period_total": sum(cur_days.values()), "last_load": {"at": iso(at), "rows": rows},
            })

    # ── errors: what failed, grouped by what went wrong ──────────────────────
    sigs: dict[str, dict[str, Any]] = {}
    err_daily = {d: Counter() for d in calendar}
    for f in sorted(failures, key=lambda f: f["run"].requested_at):
        r = f["run"]
        key, known = signature_of(r.error or "")
        lt = local(r.started_at or r.requested_at)
        if lt.date() in err_daily:
            err_daily[lt.date()][f"{f['project'].key}/{f['site']}"] += 1
        s = sigs.setdefault(key, {"key": key, "title": known.title if known else None, "known": known is not None,
                                  "sample": _short(_PREFIX.sub("", r.error or "")) or f"{r.run_status} without an error message", "count": 0,
                                  "pipelines": {}, "steps": [], "first_at": iso(r.requested_at), "last_at": None, "incident": None})
        s["count"] += 1
        step = context_of(r.error or "", f["site"]).get("step")
        if step and step not in s["steps"]:
            s["steps"].append(step)
        s["pipelines"][f"{f['project'].key}/{f['site']}"] = {"project": f["project"].key, "name": f["site"]}
        s["last_at"] = iso(r.requested_at)
        s["last_run"] = {"project": f["project"].key, "run_id": r.run_id, "pipeline": f["site"]}
        if f["incident"] is not None:
            i = f["incident"]
            s["incident"] = {"id": i.id, "project": f["project"].key, "title": i.title, "status": i.status}
        if s["title"] is None:
            code = re.search(r"ErrorCode=(\w+)", r.error or "")
            s["title"] = code.group(1) if code else None
    signatures = sorted(({**s, "pipelines": list(s["pipelines"].values())} for s in sigs.values()), key=lambda s: (-s["count"], s["key"]))
    recent = [{"at": iso(f["run"].started_at or f["run"].requested_at), "project": f["project"].key, "pipeline": f["site"],
               "run_id": f["run"].run_id, "status": f["run"].run_status, "error": _why(f["run"].error, f["site"]) or "",
               "incident_id": f["incident"].id if f["incident"] is not None else None}
              for f in sorted(failures, key=lambda f: f["run"].requested_at, reverse=True)[:12]]

    # ── the numbers on top ───────────────────────────────────────────────────
    order = {"failing": 0, "late": 1, "handled": 2, "unstable": 3, "running": 4, "healthy": 5}
    pipelines.sort(key=lambda p: (order[p["state"]], p["project_name"].lower(), p["name"].lower()))
    volumes.sort(key=lambda v: ({"none": 0, "low": 1, "high": 2}.get(v["verdict"] or "", 3), v["project_name"].lower(), v["short"].lower()))
    done_all = sum(p["ok"] + p["failed"] for p in pipelines)
    ok_all = sum(p["ok"] for p in pipelines)
    done_prev_all = sum(p["done_prev"] for p in pipelines)
    ok_prev_all = sum(p["ok_prev"] for p in pipelines)
    typ_runs = sum(p["today"]["typical_by_now"] or 0 for p in pipelines)
    typ_rows = sum(v["typical_by_now"] or 0 for v in volumes)
    summary = {
        "pipelines": len(pipelines), "quiet": quiet, "tables": len(volumes),
        "states": {k: sum(p["state"] == k for p in pipelines) for k in order},
        "success_rate": round(ok_all / done_all, 4) if done_all else None,
        "success_rate_prev": round(ok_prev_all / done_prev_all, 4) if done_prev_all else None,
        "runs": sum(p["runs"] for p in pipelines), "failed": sum(p["failed"] for p in pipelines),
        "runs_today": sum(p["today"]["ok"] for p in pipelines), "failed_today": sum(p["today"]["failed"] for p in pipelines),
        "runs_typical_by_now": round(typ_runs, 1), "rows_today": sum(v["today"] for v in volumes), "rows_typical_by_now": round(typ_rows),
        "rows_period": sum(v["period_total"] for v in volumes),
        "open_incidents": len(open_incidents), "incidents_opened": inc_opened, "incidents_resolved": inc_resolved,
        "mttr_hours": round(statistics.median(mttr), 1) if mttr else None,
    }
    if user_id is not None:  # each pipeline's Snowflake compute, from the person's connected accounts
        from ..dq.compute import pipeline_costs

        costs = pipeline_costs(session, user_id)
        for p in pipelines:
            p["snowflake"] = costs.get((p["project"], p["name"]))
    found = insights(pipelines, volumes, signatures, summary)
    for p in pipelines:
        p["last"].pop("raw_error", None)  # only needed to word the insight
    return {
        "days": days, "timezone": str(tz), "generated_at": iso(now), "today": today.isoformat(), "calendar": [d.isoformat() for d in calendar],
        "summary": summary, "insights": found,
        "pipelines": pipelines, "volumes": volumes,
        "errors": {"daily": [{"date": d.isoformat(), "total": sum(c.values()), "by": dict(c)} for d, c in err_daily.items()],
                   "signatures": signatures[:12], "recent": recent},
        "heatmap": heat, "grid": {"slot_h": slot_h, "slots": slots, "now": round((local(now) - start_local).total_seconds() / (slot_h * 3600), 2)},
        "open_incidents": sorted(open_incidents, key=lambda i: i["opened_at"] or "", reverse=True)[:10],
    }


def _grid(rs: list[Run], rows: dict[Any, list[tuple[time, float]]], site: str, start: datetime, slots: int, slot_of: Any, local: Any,
          tz: Any) -> list[dict[str, Any]]:
    """One pipeline's run map: only the slots where something happened. Runs, failures, how long they took, rows
    loaded, and the first failure's reason (to click through to)."""
    cells: dict[int, dict[str, Any]] = {}
    cell = lambda i: cells.setdefault(i, {"slot": i, "ok": 0, "failed": 0, "other": 0, "durs": [], "rows": 0.0, "error": None, "run_id": None})  # noqa: E731
    for r in rs:
        if r.requested_at < start:
            continue
        i = slot_of(local(r.started_at or r.requested_at))
        if not 0 <= i < slots:
            continue
        c = cell(i)
        kind = "ok" if r.run_status in OK else "failed" if r.run_status in FAILED else "other"
        c[kind] += 1
        if kind == "ok" and r.duration_s:
            c["durs"].append(r.duration_s)
        if kind == "failed" and c["error"] is None:
            c["error"], c["run_id"] = _why(r.error, site) or f"{r.run_status} without an error message", r.run_id
        elif c["run_id"] is None:
            c["run_id"] = r.run_id
    for d, pts in rows.items():
        for t, v in pts:
            i = slot_of(datetime.combine(d, t, tzinfo=tz))
            if 0 <= i < slots and v:
                cell(i)["rows"] += v
    out = []
    for c in sorted(cells.values(), key=lambda c: c["slot"]):
        durs = c.pop("durs")
        out.append({**c, "avg_s": round(statistics.fmean(durs), 1) if durs else None, "max_s": round(max(durs), 1) if durs else None})
    return out


def _why(error: str | None, pipeline: str) -> str | None:
    """A failure in a sentence: the known name for it and the step, or the start of Azure's message."""
    if not error:
        return None
    _, known = signature_of(error)
    step = context_of(error, pipeline).get("step")
    if known is not None:
        return f"{known.title}" + (f", in step {step}." if step else ".")
    return (f"Step {step}: " if step else "") + _short(_PREFIX.sub("", error), 140)


_PREFIX = re.compile(r"^Operation on target .*? failed:\s*")


def _every(seconds: float) -> str:
    m = seconds / 60
    return f"{round(m)} min" if m < 90 else f"{m / 60:.0f} h" if m < 36 * 60 else f"{m / 1440:.0f} days"


def insights(pipelines: list[dict[str, Any]], volumes: list[dict[str, Any]], signatures: list[dict[str, Any]], s: dict[str, Any]) -> list[dict[str, Any]]:
    """What a person should know first, most important first. Plain sentences; each points at the thing it's about."""
    out: list[dict[str, Any]] = []
    add = lambda tone, kind, title, detail, link=None, rank=0: out.append(  # noqa: E731
        {"tone": tone, "kind": kind, "title": title, "detail": detail, "link": link, "rank": rank})
    for p in pipelines:
        link = {"kind": "pipeline", "project": p["project"], "name": p["name"]}
        if p["state"] == "failing":
            add("critical", "failing", f"{p['name']} is failing", _why(p["last"].get("raw_error"), p["name"]) or f"Its last run {p['last']['status'].lower()}.",
                {"kind": "incident", "project": p["project"], "id": p["incident"]["id"]} if p["incident"] else link, 100)
        if p["cadence"]["overdue"]:
            add("critical" if p["state"] != "failing" else "warning", "late", f"{p['name']} is overdue",
                f"It usually runs every {_every(p['cadence']['interval_s'])}; the last run started {_every(p['cadence']['since_last_s'])} ago.", link, 95)
        t = p["today"]
        if t["typical_by_now"] and t["typical_by_now"] >= 2 and t["pace"] is not None and t["pace"] < 0.6 and not p["cadence"]["overdue"]:
            add("warning", "behind", f"{p['name']} is behind today",
                f"{t['ok']} successful run{'s' if t['ok'] != 1 else ''} so far; by this time it usually has {t['typical_by_now']:.0f}.", link, 70)
        if p["success_rate"] is not None and p["runs"] >= 5 and p["success_rate"] < 0.9:
            add("warning", "unreliable", f"{p['name']} succeeds {p['success_rate'] * 100:.0f}% of the time",
                f"{p['failed']} of {p['runs']} runs failed in this period"
                + (f" (was {p['success_rate_prev'] * 100:.0f}% before)." if p["success_rate_prev"] is not None else "."), link, 60)
        rt = p["runtime"] or {}
        if rt.get("verdict") == "much_slower" or rt.get("verdict") == "overdue":
            add("warning", "slow", f"{p['name']} is running slow",
                f"The last run took {rt.get('pct', 0):+d}% longer than its usual {_every(rt['median_s'])}." if rt.get("pct") is not None
                else "The current run is already past its normal time.", link, 55)
        if p["success_rate"] is not None and p["success_rate_prev"] is not None and p["success_rate"] - p["success_rate_prev"] >= 0.15                 and p["success_rate"] >= 0.9 and p["runs"] >= 5 and p["state"] in ("healthy", "unstable"):
            add("good", "better", f"{p['name']} is more reliable",
                f"{p['success_rate'] * 100:.0f}% of runs succeeded, up from {p['success_rate_prev'] * 100:.0f}% the period before.", link, 20)
    for v in volumes:
        link = {"kind": "table", "project": v["project"], "table": v["table"]}
        if v["verdict"] == "none":
            add("critical", "no_rows", f"No rows in {v['short']} today", f"By now it usually has {v['typical_by_now']:,.0f} rows ({v['pipeline']}).", link, 80)
        elif v["verdict"] == "low":
            add("warning", "low_rows", f"{v['short']} is {abs(v['pct_vs_typical']) * 100:.0f}% below usual",
                f"{v['today']:,.0f} rows so far today; by now it usually has {v['typical_by_now']:,.0f}.", link, 65)
        elif v["verdict"] == "high":
            add("info", "high_rows", f"{v['short']} got {v['pct_vs_typical'] * 100:.0f}% more rows than usual",
                f"{v['today']:,.0f} rows so far today against {v['typical_by_now']:,.0f} by now normally. A backfill, or duplicates?", link, 40)
    for g in signatures[:3]:
        if g["count"] >= 3:
            where = ", ".join(p["name"] for p in g["pipelines"][:3]) + ("…" if len(g["pipelines"]) > 3 else "")
            add("warning" if not g["known"] else "info", "recurring", f"{g['title'] or 'The same error'} happened {g['count']} times",
                f"On {where}." + (" There's a known fix: open the incident for the steps." if g["known"] else ""),
                {"kind": "incident", "project": g["incident"]["project"], "id": g["incident"]["id"]} if g["incident"] else
                {"kind": "errors"}, 50)
    if not any(i["tone"] in ("critical", "warning") for i in out) and pipelines:
        add("good", "all_good", "Everything ran as expected",
            f"All {len(pipelines)} pipelines are on pace today" + (f", {s['success_rate'] * 100:.0f}% of runs succeeded this period." if s["success_rate"] is not None else "."),
            None, 10)
    if s["success_rate"] is not None and s["success_rate_prev"] is not None and s["success_rate"] - s["success_rate_prev"] <= -0.1:
        add("warning", "worse", "Reliability dropped",
            f"{s['success_rate'] * 100:.0f}% of runs succeeded this period, down from {s['success_rate_prev'] * 100:.0f}%.", {"kind": "errors"}, 75)
    out.sort(key=lambda i: -i["rank"])
    return out[:8]


@router.get("/api/pipeline-report")
def pipeline_report(user: UserDep, session: SessionDep, days: Annotated[int, Query(ge=2, le=60)] = 7) -> dict:
    links = session.execute(
        select(Project, UserProject.role).join(UserProject, UserProject.project_id == Project.id)
        .where(UserProject.user_id == user.id, Project.active.is_(True)).order_by(Project.display_name)).all()
    return build(session, [(p, r) for p, r in links], days, user_id=user.id)
