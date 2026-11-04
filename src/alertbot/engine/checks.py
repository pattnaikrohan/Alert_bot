"""The checks a web-scraping project gets.

Every check returns `Outcome`s scoped to a site (`site:<name>`) or an output
table (`output:<id>`). Passing outcomes are kept too: they drive the health
score, and they are what resolves an incident.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from ..db import MetricObservation, Run, SchemaSnapshot
from ..timeutil import WEEKDAYS, iso, local_weekday
from .baselines import Band, band_for
from .context import ProjectContext
from .observe import TERMINAL

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Outcome:
    check: str
    scope: str
    status: str  # pass | fail | skip
    severity: str | None = None
    message: str = ""
    actual: float | None = None
    expected_low: float | None = None
    expected_high: float | None = None
    run_ref: str | None = None
    site: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == "fail"


# ── formatting helpers ────────────────────────────────────────────────────────


def _n(v: float) -> str:
    return f"{v:,.0f}"


def _pct(v: float) -> str:
    return f"{v * 100:.1f}%" if v < 0.1 else f"{v * 100:.0f}%"


def _dur(seconds: float) -> str:
    m = seconds / 60
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h"


def _basis(b: Band) -> str:
    return f"{WEEKDAYS[b.weekday]} baseline, {b.samples} runs" if b.basis == "weekday" else f"all-days baseline, {b.samples} runs"


def _site_scope(site: str) -> str:
    return f"site:{site}"


def diff_schema(old: list[dict[str, str]], new: list[dict[str, str]]) -> dict[str, list]:
    o = {c["name"].lower(): c for c in old}
    n = {c["name"].lower(): c for c in new}
    return {
        "added": [n[k]["name"] for k in n.keys() - o.keys()],
        "removed": [o[k]["name"] for k in o.keys() - n.keys()],
        "retyped": [
            {"column": n[k]["name"], "from": o[k]["type"], "to": n[k]["type"]}
            for k in n.keys() & o.keys()
            if o[k]["type"] != n[k]["type"]
        ],
    }


def schema_fingerprint(columns: list[dict[str, str]]) -> str:
    canon = json.dumps(sorted((c["name"].lower(), c["type"]) for c in columns))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


# ── the evaluator ─────────────────────────────────────────────────────────────


class Evaluator:
    def __init__(self, ctx: ProjectContext, runs: list[Run]):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.now = ctx.now
        self.tz = ctx.settings.tz
        self.grace = timedelta(minutes=ctx.settings.load_grace_minutes)
        self.by_site: dict[str, list[Run]] = defaultdict(list)
        for r in sorted(runs, key=lambda r: r.requested_at, reverse=True):
            self.by_site[r.site].append(r)
        # Sites seen in the last two weeks, so a site that silently stopped is still watched.
        known = ctx.session.scalars(
            select(Run.site)
            .where(Run.project_id == ctx.project.id, Run.requested_at >= self.now - timedelta(days=14))
            .distinct()
        ).all()
        self.sites = sorted(set(known) | set(self.by_site))

    def enabled(self, name: str) -> bool:
        return self.cfg.check(name).enabled

    def outputs_for(self, site: str) -> list:  # noqa: ARG002 - feeds override this per site
        return list(self.cfg.outputs)

    def _sev(self, name: str, computed: str) -> str:
        return self.cfg.check(name).severity or computed

    def run(self) -> list[Outcome]:
        out: list[Outcome] = []
        for site in self.sites:
            runs = self.by_site.get(site, [])
            for name, fn in (
                ("stuck_trigger", self.stuck_trigger),
                ("run_failed", self.run_failed),
                ("run_too_slow", self.run_too_slow),
                ("mismatch", self.mismatch),
            ):
                if self.enabled(name):
                    out.extend(fn(site, runs))
            out.extend(self.output_checks(site, runs))
        if self.enabled("freshness"):
            out.extend(self.freshness())
        if self.enabled("schema_drift"):
            out.extend(self.schema_drift())
        return out

    # pipeline-side checks --------------------------------------------------------

    def stuck_trigger(self, site: str, runs: list[Run]) -> list[Outcome]:
        after = int(self.cfg.check("stuck_trigger").param("after_minutes", 20))
        stuck = [r for r in runs if r.trigger_status == "pending" and not r.run_id
                 and r.requested_at < self.now - timedelta(minutes=after)]
        scope = _site_scope(site)
        if not stuck:
            return [Outcome("stuck_trigger", scope, "pass", site=site)]
        oldest = min(stuck, key=lambda r: r.requested_at)
        waited = (self.now - oldest.requested_at).total_seconds() / 60
        sev = "critical" if waited > after * 3 else "warning"
        return [Outcome(
            "stuck_trigger", scope, "fail", self._sev("stuck_trigger", sev),
            f"Trigger row {oldest.trigger_id} has been pending for {waited:.0f} min with no pipeline run "
            f"(limit {after} min).",
            actual=waited, expected_high=after, run_ref=oldest.trigger_id, site=site,
            details={"pending_rows": len(stuck), "oldest_requested_at": iso(oldest.requested_at)},
        )]

    def _latest_finished(self, runs: list[Run]) -> Run | None:
        return next((r for r in runs if r.run_status in TERMINAL or r.trigger_status == "failed"), None)

    def run_failed(self, site: str, runs: list[Run]) -> list[Outcome]:
        scope = _site_scope(site)
        last = self._latest_finished(runs)
        if last is None:
            return [Outcome("run_failed", scope, "skip", site=site, message="No finished run in the lookback window.")]
        if last.run_status in ("Failed", "Cancelled") or (last.trigger_status == "failed" and last.run_status != "Succeeded"):
            details: dict[str, Any] = {"run_id": last.run_id, "pipeline": last.pipeline, "run_status": last.run_status}
            failing = None
            if last.run_id:
                acts = self.ctx.connectors.pipeline.get_activity_runs(last.run_id)
                failing = next((a for a in acts if a.status == "Failed"), None)
                if failing:
                    details["failed_activity"] = failing.name
                    details["error"] = (failing.error or {}).get("message", "")
            what = f"activity '{failing.name}' failed" if failing else f"run {(last.run_status or 'failed').lower()}"
            err = details.get("error") or last.error or ""
            return [Outcome(
                "run_failed", scope, "fail", self._sev("run_failed", "critical"),
                f"Pipeline run for {site} failed: {what}." + (f" {err[:240]}" if err else ""),
                run_ref=last.run_id or last.trigger_id, site=site, details=details,
            )]
        return [Outcome("run_failed", scope, "pass", site=site, run_ref=last.run_id)]

    def run_too_slow(self, site: str, runs: list[Run]) -> list[Outcome]:
        scope = _site_scope(site)
        factor = float(self.cfg.check("run_too_slow").param("min_factor", 1.5))
        current = next((r for r in runs if r.run_status in ("InProgress", "Queued") and r.started_at), None)
        target = current or next((r for r in runs if r.run_status == "Succeeded" and r.duration_s), None)
        if target is None:
            return [Outcome("run_too_slow", scope, "skip", site=site)]
        band = band_for(self.ctx, "_pipeline", site, "duration_s", "", local_weekday(target.requested_at, self.tz),
                        before=target.started_at or target.requested_at, exclude_run=target.run_id,
                        min_factor=factor, persist=True)
        if band is None:
            return [Outcome("run_too_slow", scope, "skip", site=site, message="Still learning normal duration.")]
        elapsed = (self.now - target.started_at).total_seconds() if current else float(target.duration_s or 0)
        if elapsed <= band.high:
            return [Outcome("run_too_slow", scope, "pass", site=site, actual=elapsed, expected_high=band.high, run_ref=target.run_id)]
        if current:
            msg = (f"Run has been going for {_dur(elapsed)}; it usually finishes in {_dur(band.median)} "
                   f"(limit {_dur(band.high)}). Likely to miss its usual completion time.")
        else:
            msg = f"Last run took {_dur(elapsed)} against a usual {_dur(band.median)} (limit {_dur(band.high)})."
        sev = "critical" if elapsed > band.median * 3 else "warning"
        return [Outcome("run_too_slow", scope, "fail", self._sev("run_too_slow", sev), msg, actual=elapsed,
                        expected_low=band.low, expected_high=band.high, run_ref=target.run_id, site=site,
                        details={"in_progress": bool(current), "baseline": asdict(band)})]

    def mismatch(self, site: str, runs: list[Run]) -> list[Outcome]:
        scope = _site_scope(site)
        out_cfg = self.cfg.outputs[0]
        last = self._latest_finished(runs)
        if last is not None and last.trigger_status == "succeeded":
            if last.run_status in ("Failed", "Cancelled"):
                return [Outcome("mismatch", scope, "fail", self._sev("mismatch", "critical"),
                                f"Trigger row says succeeded, but pipeline run {last.run_id} {last.run_status.lower()}.",
                                run_ref=last.run_id, site=site)]
            if last.observed and (last.rows_loaded or 0) == 0:
                return [Outcome("mismatch", scope, "fail", self._sev("mismatch", "critical"),
                                "Trigger row and pipeline both say succeeded, but no rows landed in "
                                f"{out_cfg.table}.", actual=0, run_ref=last.run_id, site=site)]

        # Rows that landed outside every known run window: something wrote without a trigger.
        start = self.now - timedelta(hours=24)
        windows = [
            ((r.started_at or r.requested_at), (r.ended_at + self.grace) if r.ended_at else self.now)
            for r in runs if r.run_id
        ]
        orphans = self.ctx.connectors.source(out_cfg.source).rows_outside_windows(out_cfg, site, start, self.now, windows)
        if orphans:
            return [Outcome("mismatch", scope, "fail", self._sev("mismatch", "warning"),
                            f"{_n(orphans)} rows landed in {out_cfg.table} in the last 24 h with no matching trigger row.",
                            actual=orphans, site=site, details={"orphan_rows": orphans})]
        return [Outcome("mismatch", scope, "pass", site=site)]

    # output-side checks ----------------------------------------------------------

    def _obs(self, run_ref: str, output_id: str) -> dict[tuple[str, str], float]:
        rows = self.ctx.session.scalars(
            select(MetricObservation).where(
                MetricObservation.project_id == self.ctx.project.id,
                MetricObservation.output_id == output_id,
                MetricObservation.run_ref == run_ref,
            )
        )
        return {(o.metric, o.field): o.value for o in rows}

    def output_checks(self, site: str, runs: list[Run]) -> list[Outcome]:
        scope = _site_scope(site)
        run = next((r for r in runs if r.observed), None)
        names = ("row_count", "empty_site", "null_spike", "duplicates")
        if run is None:
            return [Outcome(n, scope, "skip", site=site, message="No measured run yet.") for n in names if self.enabled(n)]
        ref = run.run_id or run.trigger_id
        weekday = local_weekday(run.requested_at, self.tz)
        results: list[Outcome] = []
        for out in self.outputs_for(site):
            m = self._obs(ref, out.id)
            rows = m.get(("row_count", ""), 0.0)

            def band(metric: str, fld: str = "", _out=out) -> Band | None:
                return band_for(self.ctx, _out.id, site, metric, fld, weekday, before=run.ended_at,
                                exclude_run=ref, persist=True)

            rb = band("row_count")
            base_info = {"run_id": run.run_id, "output": out.id, "table": out.table, "weekday": WEEKDAYS[weekday]}

            if self.enabled("empty_site"):
                if rows == 0 and (rb is None or rb.median > 0):
                    results.append(Outcome("empty_site", scope, "fail", self._sev("empty_site", "critical"),
                                           f"{site} returned zero rows" + (f"; it normally returns about {_n(rb.median)}." if rb else "."),
                                           actual=0, expected_low=rb.low if rb else None, expected_high=rb.high if rb else None,
                                           run_ref=ref, site=site, details=base_info))
                else:
                    results.append(Outcome("empty_site", scope, "pass", site=site, actual=rows, run_ref=ref))

            if self.enabled("row_count"):
                results.append(self._row_count(scope, site, ref, rows, rb, base_info))

            if self.enabled("null_spike"):
                results.append(self._null_spike(scope, site, ref, rows, m, band, out.required_fields, base_info))

            if self.enabled("duplicates"):
                rate = m.get(("duplicate_rate", ""))
                max_rate = float(self.cfg.check("duplicates").param("max_rate", 0.01))
                db = band("duplicate_rate") if rate is not None else None
                limit = max(max_rate, db.high if db else 0.0)
                if rate is None or rows == 0:
                    results.append(Outcome("duplicates", scope, "skip", site=site, run_ref=ref))
                elif rate > limit:
                    results.append(Outcome("duplicates", scope, "fail", self._sev("duplicates", "warning"),
                                           f"{_pct(rate)} of rows repeat a key ({', '.join(out.key)}); limit {_pct(limit)}.",
                                           actual=rate, expected_high=limit, run_ref=ref, site=site, details=base_info))
                else:
                    results.append(Outcome("duplicates", scope, "pass", site=site, actual=rate, expected_high=limit, run_ref=ref))
        return results

    def _row_count(self, scope: str, site: str, ref: str, rows: float, rb: Band | None, info: dict) -> Outcome:
        p = self.cfg.check("row_count")
        pin_min, pin_max = p.param("min"), p.param("max")
        if rows == 0 and self.enabled("empty_site"):
            return Outcome("row_count", scope, "skip", site=site, run_ref=ref, message="Covered by empty_site.")
        low = max(rb.low, float(pin_min)) if rb and pin_min is not None else (float(pin_min) if pin_min is not None else (rb.low if rb else None))
        high = min(rb.high, float(pin_max)) if rb and pin_max is not None else (float(pin_max) if pin_max is not None else (rb.high if rb else None))
        if low is None and high is None:
            return Outcome("row_count", scope, "skip", site=site, run_ref=ref, actual=rows, message="Still learning normal volume.")
        details = {**info, "baseline": asdict(rb) if rb else None, "pinned": {"min": pin_min, "max": pin_max}}
        basis = _basis(rb) if rb else "pinned rule"
        expected = f"{_n(low or 0)}–{_n(high)}" if high is not None else f"at least {_n(low or 0)}"
        if low is not None and rows < low:
            drop = 1 - rows / rb.median if rb and rb.median else 1.0
            sev = "critical" if drop >= 0.5 else "warning"
            msg = f"{_n(rows)} rows against an expected {expected} ({basis})" + (f", {drop:.0%} below normal." if rb else ".")
            return Outcome("row_count", scope, "fail", self._sev("row_count", sev), msg, rows, low, high, ref, site,
                           {**details, "direction": "low", "drop": drop})
        if high is not None and rows > high:
            rise = rows / rb.median - 1 if rb and rb.median else 0.0
            msg = f"{_n(rows)} rows against an expected {expected} ({basis})" + (f", {rise:.0%} above normal." if rb else ".")
            return Outcome("row_count", scope, "fail", self._sev("row_count", "warning"), msg, rows, low, high, ref, site,
                           {**details, "direction": "high", "rise": rise})
        return Outcome("row_count", scope, "pass", site=site, actual=rows, expected_low=low, expected_high=high, run_ref=ref, details=details)

    def _null_spike(self, scope, site, ref, rows, m, band, fields, info) -> Outcome:  # noqa: ANN001
        if rows == 0:
            return Outcome("null_spike", scope, "skip", site=site, run_ref=ref)
        spikes = []
        checked = 0
        for f in fields:
            rate = m.get(("null_rate", f))
            if rate is None:
                continue
            b = band("null_rate", f)
            limit = b.high if b else 0.05
            checked += 1
            if rate > limit:
                spikes.append({"field": f, "rate": rate, "limit": limit, "normal": b.median if b else None})
        if not checked:
            return Outcome("null_spike", scope, "skip", site=site, run_ref=ref)
        if not spikes:
            return Outcome("null_spike", scope, "pass", site=site, run_ref=ref)
        worst = max(spikes, key=lambda s: s["rate"])
        parts = [
            f"{s['field']} is empty in {_pct(s['rate'])} of rows (normally {_pct(s['normal']) if s['normal'] is not None else 'n/a'}, limit {_pct(s['limit'])})"
            for s in sorted(spikes, key=lambda s: -s["rate"])
        ]
        sev = "critical" if worst["rate"] >= 0.25 else "warning"
        return Outcome("null_spike", scope, "fail", self._sev("null_spike", sev), "; ".join(parts) + ".",
                       actual=worst["rate"], expected_high=worst["limit"], run_ref=ref, site=site,
                       details={**info, "fields": spikes})

    def freshness(self) -> list[Outcome]:
        p = self.cfg.check("freshness")
        out_cfg = self.cfg.outputs[0]
        grace = timedelta(minutes=int(p.param("grace_minutes", 60)))
        last = self.ctx.connectors.source(out_cfg.source).last_loaded_by_site(out_cfg, self.now - timedelta(days=14))
        results = []
        for site in self.sites:
            scope = _site_scope(site)
            every = p.param("expected_every_minutes")
            if every is None:
                times = sorted(r.requested_at for r in self.by_site.get(site, []))
                gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:], strict=False)]
                every = statistics.median(gaps) if gaps else None
            if every is None:
                results.append(Outcome("freshness", scope, "skip", site=site, message="Cadence unknown yet."))
                continue
            limit = timedelta(minutes=float(every)) + grace
            seen = last.get(site)
            age = (self.now - seen) if seen else None
            if age is not None and age <= limit:
                results.append(Outcome("freshness", scope, "pass", site=site, actual=age.total_seconds() / 3600,
                                       expected_high=limit.total_seconds() / 3600))
                continue
            hours = age.total_seconds() / 3600 if age else None
            sev = "critical" if age is None or age > limit * 2 else "warning"
            when = f"since {seen:%a %d %b %H:%M} UTC ({hours:.1f} h ago)" if seen else "in the last 14 days"
            results.append(Outcome("freshness", scope, "fail", self._sev("freshness", sev),
                                   f"No new rows for {site} {when}; expected at least every {float(every) / 60:.0f} h.",
                                   actual=hours, expected_high=limit.total_seconds() / 3600, site=site,
                                   details={"last_loaded_at": iso(seen)}))
        return results

    def schema_drift(self) -> list[Outcome]:
        results = []
        for out in self.cfg.outputs:
            scope = f"output:{out.id}"
            current = [asdict(c) for c in self.ctx.connectors.source(out.source).read_schema(out)]
            snap = self.ctx.session.scalars(
                select(SchemaSnapshot)
                .where(SchemaSnapshot.project_id == self.ctx.project.id, SchemaSnapshot.output_id == out.id)
                .order_by(SchemaSnapshot.captured_at.desc(), SchemaSnapshot.id.desc())
                .limit(1)
            ).first()
            fp = schema_fingerprint(current)
            if snap is None:
                self.ctx.session.add(SchemaSnapshot(project_id=self.ctx.project.id, output_id=out.id, columns=current,
                                                    fingerprint=fp, captured_at=self.now))
                results.append(Outcome("schema_drift", scope, "pass", message="Schema recorded as the baseline."))
                continue
            if snap.fingerprint == fp:
                results.append(Outcome("schema_drift", scope, "pass"))
                continue
            d = diff_schema(snap.columns, current)
            parts = []
            if d["removed"]:
                parts.append("removed " + ", ".join(d["removed"]))
            if d["retyped"]:
                parts.append("retyped " + ", ".join(f"{r['column']} ({r['from']} → {r['to']})" for r in d["retyped"]))
            if d["added"]:
                parts.append("added " + ", ".join(d["added"]))
            sev = "critical" if d["removed"] or d["retyped"] else "warning"
            results.append(Outcome("schema_drift", scope, "fail", self._sev("schema_drift", sev),
                                   f"{out.table} changed: {'; '.join(parts)}.",
                                   details={**d, "table": out.table, "output": out.id,
                                            "baseline_captured_at": iso(snap.captured_at), "current": current}))
        return results


def accept_schema(ctx: ProjectContext, output_id: str, now: datetime) -> SchemaSnapshot:
    """Owner action: accept the table's current schema as the new baseline."""
    if ctx.cfg.kind == "feeds":
        feed = next(f for f in ctx.cfg.feeds if f.table.lower() == output_id)
        current = [asdict(c) for c in ctx.connectors.source(feed.source).read_schema(feed.table)]
    else:
        out = next(o for o in ctx.cfg.outputs if o.id == output_id)
        current = [asdict(c) for c in ctx.connectors.source(out.source).read_schema(out)]
    snap = SchemaSnapshot(project_id=ctx.project.id, output_id=output_id, columns=current,
                          fingerprint=schema_fingerprint(current), captured_at=now)
    ctx.session.add(snap)
    return snap
