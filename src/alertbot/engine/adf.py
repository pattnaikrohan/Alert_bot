"""ADF projects: watch Azure Data Factory pipelines directly (no trigger table).

Each pipeline is one watched item (a "site"). Every cycle:
    query the factory's run history         → `runs` (one row per pipeline run, keyed by the ADF run id)
    finished runs, once                     → copy stats from their activity runs (rows read / copied / skipped)
                                              and duration → metric observations, so the bot learns normal volume
    checks                                  → run failed · run too slow (or stuck) · rows copied unusually low/high
                                              or zero · missed its schedule (from the factory's own triggers)
"""

from __future__ import annotations

from datetime import UTC, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from ..config import ADF_OUTPUT_ID
from ..connectors.adf import AdfPipeline, Schedule, copy_stats
from ..db import MetricObservation, Run
from ..timeutil import iso, local_weekday
from .baselines import band_for
from .checks import Evaluator, Outcome, _dur
from .context import ProjectContext
from .observe import TERMINAL

TABLE_PREFIX = "table:"  # metric output id for one target table, e.g. "table:DB_PROD.RAW_TERMINAL.BA_ID"

STATE = {"Succeeded": "succeeded", "Failed": "failed", "Cancelled": "failed", "InProgress": "running",
         "Queued": "pending", "Cancelling": "running"}


def _adf(ctx: ProjectContext) -> AdfPipeline:
    return ctx.connectors.pipeline  # type: ignore[return-value]


def sync_adf_runs(ctx: ProjectContext, lookback_hours: int) -> list[Run]:
    adf = _adf(ctx)
    since = ctx.now - timedelta(hours=lookback_hours)
    pipelines = adf.watched_pipelines()
    if hasattr(adf, "inventory"):
        refresh_inventory(ctx)
    remote = adf.query_runs(since, ctx.now + timedelta(minutes=5), pipelines=pipelines) if pipelines else []
    existing = {r.trigger_id: r for r in ctx.session.scalars(
        select(Run).where(Run.project_id == ctx.project.id, Run.requested_at >= since - timedelta(days=2)))}
    runs: list[Run] = []
    for pr in remote:
        run = existing.get(pr.run_id)
        if run is None:
            run = Run(project_id=ctx.project.id, trigger_id=pr.run_id, site=pr.pipeline, requested_at=pr.started_at or ctx.now)
            ctx.session.add(run)
        run.run_id = pr.run_id
        run.site = pr.pipeline
        run.pipeline = pr.invoked_by or pr.pipeline
        run.run_status = pr.status
        run.trigger_status = STATE.get(pr.status, "unknown")
        run.started_at, run.ended_at, run.duration_s = pr.started_at, pr.ended_at, pr.duration_s
        run.error = pr.message or None
        runs.append(run)
    # runs still open locally but not updated in the window (e.g. long-running): keep them in view
    seen = {r.trigger_id for r in runs}
    runs += [r for r in existing.values() if r.trigger_id not in seen and r.requested_at >= since]
    ctx.session.flush()
    return runs


INVENTORY_EVERY = timedelta(minutes=10)


def refresh_inventory(ctx: ProjectContext, force: bool = False) -> None:
    """Every pipeline / workflow the project can see, run or not (with folder, steps, triggers, enabled), for the app."""
    from ..db import BotState

    key = f"inventory:{ctx.key}"
    row = ctx.session.get(BotState, key)
    if row is not None and not force and ctx.now - row.updated_at < INVENTORY_EVERY and not (row.value or {}).get("error"):
        return
    try:
        adf = _adf(ctx)
        if hasattr(adf, "forget"):
            adf.forget()  # read Azure as it is now (pipelines, datasets, linked services may all have changed)
        items = adf.inventory()
        value: dict[str, Any] = {"items": items, "at": iso(ctx.now)}
        if items:
            close_gone(ctx, {i["name"] for i in items}, items)
            from ..dq.service import reconcile_mappings

            reconcile_mappings(ctx.session, ctx.project, items, ctx.now)
    except Exception as exc:  # noqa: BLE001 - the run checks still go ahead
        value = {**((row.value or {}) if row else {}), "error": str(exc)[:300], "at": iso(ctx.now)}
    if row is None:
        row = BotState(key=key)
        ctx.session.add(row)
    row.value, row.updated_at = value, ctx.now
    ctx.session.flush()


def loaded_tables(items: list[dict[str, Any]]) -> tuple[set[str], bool]:
    """The tables the pipelines' definitions load now (DB.SCHEMA.TABLE in capitals), and whether any load a table whose
    name is only decided at run time (then a table missing from the set may still be loaded)."""
    tables: set[str] = set()
    dynamic = False
    for item in items:
        if item.get("type", "pipeline") == "pipeline" and item.get("lineage") is None:
            dynamic = True  # its definition couldn't be read: it may load anything
        for step in item.get("lineage") or []:
            if step.get("type") == "Delete":
                continue
            for t in step.get("writes") or []:
                if t.get("kind") != "table":
                    continue
                if t.get("dynamic"):
                    dynamic = True
                elif t.get("table"):
                    tables.add(str(t["table"]).upper())
    return tables, dynamic


def close_gone(ctx: ProjectContext, names: set[str], items: list[dict[str, Any]] | None = None) -> None:
    """What an open incident watches is gone from Azure, so it can't resolve by itself: a pipeline renamed or deleted, or
    a table no pipeline loads any more (the definitions changed). Close those, saying why; the register keeps them."""
    from ..db import Incident, IncidentEvent
    from .incidents import BOT

    tables, dynamic = loaded_tables(items) if items is not None else (set(), True)
    for inc in ctx.session.scalars(select(Incident).where(Incident.project_id == ctx.project.id, Incident.status.in_(("open", "acknowledged")))):
        kind, _, name = inc.scope.partition(":")
        if kind == "site" and name not in names:
            why = f"{name} is no longer in Azure (renamed or deleted), so this incident was closed."
        elif kind == "table" and items is not None and not dynamic and name.count(".") == 2 and name.upper() not in tables:
            why = f"No pipeline in Azure loads {name.split('.')[-1]} any more (the pipelines were changed), so this incident was closed."
        else:
            continue
        inc.status, inc.resolved_at, inc.updated_at = "resolved", ctx.now, ctx.now
        inc.resolved_by, inc.resolution = BOT, why
        ctx.session.add(IncidentEvent(incident=inc, kind="resolved", at=ctx.now, message=why, data={"by": BOT}))


def inventory_of(session: Any, project_key: str) -> dict[str, Any]:
    from ..db import BotState

    row = session.get(BotState, f"inventory:{project_key}")
    return row.value if row else {}


def table_loads(adf: AdfPipeline, pipeline: str, acts: list[Any]) -> dict[str, dict[str, Any]]:
    """Rows each target table received in one run, from its successful copy steps (summed over loop iterations)."""
    try:
        targets = adf.step_targets(pipeline)
    except Exception:  # noqa: BLE001 - without the definition, name tables after their copy steps
        targets = {}
    loads: dict[str, dict[str, Any]] = {}
    for a in acts:
        if a.type != "Copy" or a.status != "Succeeded":
            continue
        s = copy_stats(a.output)
        if not s or s["rows_copied"] is None:
            continue
        t = targets.get(a.name) or {"table": a.name.upper(), "env": None, "kind": "dataset"}
        cur = loads.setdefault(t["table"], {"rows_read": 0, "rows_copied": 0, "rows_skipped": 0, "steps": [], **t})
        cur["rows_read"] += int(s["rows_read"] or 0)
        cur["rows_copied"] += int(s["rows_copied"] or 0)
        cur["rows_skipped"] += int(s["rows_skipped"] or 0)
        if a.name not in cur["steps"]:
            cur["steps"].append(a.name)
    return loads


def observe_adf_runs(ctx: ProjectContext, runs: list[Run]) -> int:
    """Once per finished run: rows each target table received (even when another step failed the run), the
    pipeline's total rows copied and its duration."""
    adf = _adf(ctx)
    measured = 0
    for run in runs:
        if run.observed or run.run_status not in TERMINAL or not run.run_id:
            continue
        acts = adf.get_activity_runs(run.run_id)
        stats = [s for s in (copy_stats(a.output) for a in acts) if s]
        weekday = local_weekday(run.requested_at, ctx.settings.tz)
        values: list[tuple[str, str, float]] = []
        per_table = table_loads(adf, run.site, acts)
        if run.run_status == "Succeeded":
            if stats:
                copied = sum(int(s["rows_copied"] or 0) for s in stats)
                values += [("row_count", "", copied),
                           ("rows_read", "", sum(int(s["rows_read"] or 0) for s in stats)),
                           ("rows_skipped", "", sum(int(s["rows_skipped"] or 0) for s in stats))]
                run.rows_loaded = copied
            if run.duration_s is not None:
                values.append(("_duration", "", run.duration_s))
        ctx.session.query(MetricObservation).filter(
            MetricObservation.project_id == ctx.project.id, MetricObservation.run_ref == run.run_id).delete()
        at = run.ended_at or run.requested_at
        ctx.session.add_all(
            MetricObservation(project_id=ctx.project.id, output_id="_pipeline" if metric == "_duration" else ADF_OUTPUT_ID,
                              site=run.site, metric="duration_s" if metric == "_duration" else metric, field=field,
                              run_ref=run.run_id, weekday=weekday, value=float(v), observed_at=at)
            for metric, field, v in values
        )
        ctx.session.add_all(
            MetricObservation(project_id=ctx.project.id, output_id=f"{TABLE_PREFIX}{table}", site=run.site, metric=metric,
                              field="", run_ref=run.run_id, weekday=weekday, value=float(load[key]), observed_at=at)
            for table, load in per_table.items()
            for metric, key in (("row_count", "rows_copied"), ("rows_read", "rows_read"), ("rows_skipped", "rows_skipped"))
        )
        run.observed = True
        measured += 1
    ctx.session.flush()
    return measured


class AdfEvaluator(Evaluator):
    """Pipeline checks from the scrape evaluator (failed / slow / volume vs learned normal) plus schedule checks."""

    def __init__(self, ctx: ProjectContext, runs: list[Run]):
        super().__init__(ctx, runs)
        self.adf = _adf(ctx)
        watched = set(self.adf.watched_pipelines())
        self.sites = sorted((set(self.sites) | watched) & watched) if watched else self.sites
        self._schedules: dict[str, list[Schedule]] | None = None

    def outputs_for(self, site: str) -> list[Any]:
        # only pipelines that copy data have a volume to judge
        return [SimpleNamespace(id=ADF_OUTPUT_ID, key=[], table=site, required_fields=[], group_by=None)]

    def output_checks(self, site: str, runs: list[Run]) -> list[Outcome]:
        measured = next((r for r in runs if r.observed and r.run_status == "Succeeded"), None)
        if measured is None or not self._obs(measured.run_id or "", ADF_OUTPUT_ID):
            return []  # no copy activity → nothing to count
        ok = [r for r in runs if r.run_status == "Succeeded"]  # a failed run copies nothing: that's run_failed's job
        return [o for o in super().output_checks(site, ok) if o.check in ("row_count", "empty_site")]

    def run(self) -> list[Outcome]:
        out: list[Outcome] = []
        for site in self.sites:
            runs = self.by_site.get(site, [])
            for name, fn in (("run_failed", self.run_failed), ("run_too_slow", self.run_too_slow), ("sla_risk", self.sla_risk)):
                if self.enabled(name):
                    out.extend(fn(site, runs))
            out.extend(self.output_checks(site, runs))
            if self.enabled("missed_run"):
                out.extend(self.missed_schedule(site, runs))
            out.extend(self.table_checks(site, runs))
        if self.ctx.quality is not None:
            from .quality import judge_quality

            out.extend(judge_quality(self.ctx, self.ctx.quality))
        return out

    def sla_risk(self, site: str, runs: list[Run]) -> list[Outcome]:
        """Predict, while a run is still going, whether it will finish late: from how many of its steps are done so far
        (at this pace it ends at …) against its usual finish, or the pipeline's SLA time when one is set."""
        scope = f"site:{site}"
        current = next((r for r in runs if r.run_status in ("InProgress", "Queued") and r.started_at and r.run_id), None)
        if current is None:
            return []
        band = band_for(self.ctx, "_pipeline", site, "duration_s", "", local_weekday(current.requested_at, self.tz),
                        before=current.started_at, exclude_run=current.run_id)
        if band is None:
            return [Outcome("sla_risk", scope, "skip", site=site, message="Still learning how long this pipeline takes.")]
        try:
            top = [n["name"] for n in self.adf._definition(site)]  # type: ignore[attr-defined]
            acts = self.ctx.connectors.pipeline.get_activity_runs(current.run_id)
        except Exception as exc:  # noqa: BLE001 - a prediction is a bonus
            return [Outcome("sla_risk", scope, "skip", site=site, message=f"Couldn't read progress: {exc}")]
        finished = {a.name for a in acts if a.status in ("Succeeded", "Failed", "Skipped", "Cancelled")}
        done = sum(1 for n in top if n in finished)
        elapsed = (self.now - current.started_at).total_seconds()
        if not top or done == 0 or done >= len(top) or elapsed < 60:
            return [Outcome("sla_risk", scope, "skip", site=site, message="Too early to predict.")]
        eta = current.started_at + timedelta(seconds=elapsed * len(top) / done)
        usual = current.started_at + timedelta(seconds=band.median)
        latest = current.started_at + timedelta(seconds=band.high)
        deadline, label = latest, "its usual finish"
        if sla := self.cfg.pipeline.sla.get(site) if self.cfg.pipeline else None:
            hh, mm = (int(x) for x in sla.split(":"))
            local = current.started_at.replace(tzinfo=UTC).astimezone(self.tz)
            d = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            deadline, label = (d if d > local else d + timedelta(days=1)).astimezone(UTC).replace(tzinfo=None), f"its SLA ({sla})"
        details = {"run_id": current.run_id, "started_at": iso(current.started_at), "steps_done": done, "steps": len(top),
                   "expected_finish": iso(eta), "usual_finish": iso(usual), "deadline": iso(deadline), "deadline_label": label}
        if eta <= deadline:
            return [Outcome("sla_risk", scope, "pass", site=site, run_ref=current.run_id, details=details)]
        fmt = lambda dt: dt.replace(tzinfo=UTC).astimezone(self.tz).strftime("%H:%M")  # noqa: E731
        late = (eta - deadline).total_seconds()
        return [Outcome("sla_risk", scope, "fail", self._sev("sla_risk", "critical" if late > band.median else "warning"),
                        f"{site} is likely to miss {label}: normally done by {fmt(usual)}; {done} of {len(top)} steps finished "
                        f"after {_dur(elapsed)}, so at this pace it ends about {fmt(eta)} ({_dur(late)} late).",
                        actual=elapsed * len(top) / done, expected_high=(deadline - current.started_at).total_seconds(),
                        run_ref=current.run_id, site=site, details=details)]

    def table_checks(self, site: str, runs: list[Run]) -> list[Outcome]:
        """Each table the pipeline loads, judged on its latest load against its own learned normal."""
        recent = {r.run_id for r in runs if r.run_id}
        if not recent:
            return []
        obs = self.ctx.session.scalars(
            select(MetricObservation).where(
                MetricObservation.project_id == self.ctx.project.id, MetricObservation.site == site,
                MetricObservation.output_id.like(f"{TABLE_PREFIX}%"), MetricObservation.run_ref.in_(recent))
            .order_by(MetricObservation.observed_at.desc()))
        latest: dict[str, dict[str, MetricObservation]] = {}
        for o in obs:
            per = latest.setdefault(o.output_id, {})
            if o.metric not in per and (not per or next(iter(per.values())).run_ref == o.run_ref):
                per[o.metric] = o
        item = next((i for i in inventory_of(self.ctx.session, self.ctx.key).get("items") or [] if i.get("name") == site), None)
        current, unknown = loaded_tables([item]) if item is not None else (set(), True)
        out: list[Outcome] = []
        for output_id, m in latest.items():
            table = output_id.removeprefix(TABLE_PREFIX)
            scope = f"table:{table}"
            if not unknown and table.count(".") == 2 and table.upper() not in current:
                continue  # Azure's definition of this pipeline doesn't load it any more: an old load isn't news
            rc = m.get("row_count")
            if rc is None:
                continue
            info = {"table": table, "pipeline": site, "run_id": rc.run_ref,
                    "rows_read": m["rows_read"].value if "rows_read" in m else None,
                    "rows_skipped": m["rows_skipped"].value if "rows_skipped" in m else None}
            band = band_for(self.ctx, output_id, site, "row_count", "", rc.weekday, before=rc.observed_at,
                            exclude_run=rc.run_ref, persist=True)
            if self.enabled("empty_site") and rc.value == 0 and band is not None and band.median > 0:
                out.append(Outcome("empty_site", scope, "fail", self._sev("empty_site", "critical"),
                                   f"{table} received 0 rows from {site}; it usually gets about {band.median:,.0f}.",
                                   actual=0, expected_low=band.low, expected_high=band.high, run_ref=rc.run_ref, site=site, details=info))
            elif self.enabled("row_count"):
                o = self._row_count(scope, site, rc.run_ref, rc.value, band, info)
                if o.status == "fail":
                    o.message = f"{table}: {o.message}"
                out.append(o)
            skipped = info["rows_skipped"] or 0
            if self.enabled("rows_skipped"):
                if skipped > 0:
                    out.append(Outcome("rows_skipped", scope, "fail", self._sev("rows_skipped", "warning"),
                                       f"{table}: the copy skipped {skipped:,.0f} of {info['rows_read'] or 0:,.0f} rows it read (incompatible rows).",
                                       actual=skipped, expected_high=0, run_ref=rc.run_ref, site=site, details=info))
                else:
                    out.append(Outcome("rows_skipped", scope, "pass", site=site, actual=0, run_ref=rc.run_ref))
        return out

    def missed_schedule(self, site: str, runs: list[Run]) -> list[Outcome]:
        scope = f"site:{site}"
        if self._schedules is None:
            try:
                self._schedules = self.adf.schedules()
            except Exception as exc:  # noqa: BLE001 - schedules are a bonus; failures/volume checks still run
                self._schedules = {}
                return [Outcome("missed_run", scope, "skip", site=site, message=f"Couldn't read triggers: {exc}")]
        sched = self._schedules.get(site)
        if not sched:
            return [Outcome("missed_run", scope, "skip", site=site, message="No active schedule trigger (manual or event-driven).")]
        every = min(s.every for s in sched)
        grace = timedelta(minutes=int(self.cfg.check("missed_run").param("grace_minutes", 30)))
        limit = every + max(grace, every * 0.25)
        last = max((r.requested_at for r in runs), default=None)
        if last is None:
            last = self.ctx.session.scalar(
                select(Run.requested_at).where(Run.project_id == self.ctx.project.id, Run.site == site)
                .order_by(Run.requested_at.desc()).limit(1))
        age = (self.now - last) if last else None
        names = ", ".join(sorted({s.trigger for s in sched}))
        if age is not None and age <= limit:
            return [Outcome("missed_run", scope, "pass", site=site, actual=age.total_seconds() / 3600, expected_high=limit.total_seconds() / 3600)]
        hours = every.total_seconds() / 3600
        sev = "critical" if age is None or age > limit * 2 else "warning"
        when = f"its last run started {age.total_seconds() / 3600:.1f} h ago" if age else "it hasn't run in the lookback window"
        return [Outcome("missed_run", scope, "fail", self._sev("missed_run", sev),
                        f"{site} should run every {hours:.1f} h (trigger {names}), but {when}.",
                        actual=age.total_seconds() / 3600 if age else None, expected_high=limit.total_seconds() / 3600, site=site,
                        details={"triggers": names, "every_hours": hours, "last_run_at": iso(last)})]
