"""Scenarios: inject a specific kind of trouble (or recovery) into a fake estate.

Times are relative to now, and every injected run is placed after the site's
latest run so the bot treats it as the newest one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa

from ..config import ProjectConfig
from ..settings import get_settings
from ..timeutil import utcnow
from .estate import Estate, Outcome


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    apply: Callable[[Estate, str | None], str]


DEFAULT_SITE = {"slipstream": "Site A", "csdw": "Retailer South"}


def _latest_activity(est: Estate, site: str) -> datetime | None:
    """When the site's most recent run finished (or was last touched)."""
    tt = est.cfg.trigger_table
    with est.engine.connect() as conn:
        return conn.execute(
            sa.select(sa.func.max(est.trigger.c.updated_at)).where(est.trigger.c[tt.site_column] == site)
        ).scalar()


def _finished_slot(est: Estate, site: str, factor: float = 1.0, leave_room: bool = True) -> tuple[datetime, float]:
    """A request time (and duration factor) so the run is finished and measurable now.

    Runs for one site never overlap. Trouble scenarios finish one run-length early,
    leaving room for a follow-up (e.g. `recover`) that is still in the past.
    """
    spec = est.site(site)
    target_end = utcnow() - timedelta(minutes=get_settings().load_grace_minutes + 2)
    if leave_room:
        target_end -= timedelta(minutes=spec.duration_min * 1.15 + 3)
    span = timedelta(minutes=spec.duration_min * factor * 1.15) + timedelta(seconds=75)
    requested = target_end - span
    last = _latest_activity(est, spec.name)
    if last is not None and requested <= last:
        requested = last + timedelta(minutes=1)
        room = (target_end - requested - timedelta(seconds=75)).total_seconds() / 60
        factor = max(0.1, room / (spec.duration_min * 1.15))
    return requested, factor


def _site(est: Estate, site: str | None) -> str:
    return est.site(site or DEFAULT_SITE.get(est.cfg.project, est.spec.sites[0].name)).name


def _incomplete(est: Estate, site: str | None) -> str:
    s = _site(est, site)
    field = "price" if "price" in est.out_cfg.required_fields else est.out_cfg.required_fields[0]
    at, f = _finished_slot(est, s)
    r = est.emit(s, at, Outcome(row_factor=0.12, duration_factor=f, null_overrides={field: 0.63}, selector_misses=640))
    return f"{s}: run {r['run_id'][:8]} succeeded with {r['rows']} rows (~88% short) and {field} null in ~63% of rows"


def _recover(est: Estate, site: str | None) -> str:
    s = _site(est, site)
    at, f = _finished_slot(est, s, leave_room=False)
    r = est.emit(s, at, Outcome(duration_factor=f))
    return f"{s}: healthy re-run {r['run_id'][:8]} with {r['rows']} rows"


def _blocked(est: Estate, site: str | None) -> str:
    s = _site(est, site)
    at, f = _finished_slot(est, s)
    r = est.emit(s, at, Outcome(kind="blocked", http_status=403, duration_factor=f * 0.4))
    return f"{s}: run {r['run_id'][:8]} 'succeeded' but the site returned HTTP 403 and no rows landed"


def _failed(est: Estate, site: str | None) -> str:
    s = _site(est, site)
    at, f = _finished_slot(est, s, factor=0.5)
    r = est.emit(s, at, Outcome(kind="failed", http_status=504, duration_factor=f,
                                error="Scraper timed out after 300 seconds waiting for the origin"))
    return f"{s}: run {r['run_id'][:8]} failed in 'Execute scraper' (HTTP 504 timeout)"


def _stuck(est: Estate, site: str | None) -> str:
    s = _site(est, site)
    after = int(est.cfg.check("stuck_trigger").param("after_minutes", 20))
    r = est.emit(s, utcnow() - timedelta(minutes=after * 2 + 5), Outcome(kind="stuck"))
    return f"{s}: trigger row {r['trigger_id']} pending for {after * 2 + 5} min with no pipeline run"


def _slow(est: Estate, site: str | None) -> str:
    s = _site(est, site)
    spec = est.site(s)
    r = est.emit(s, utcnow() - timedelta(minutes=spec.duration_min * 2.6), Outcome(kind="running"))
    return f"{s}: run {r['run_id'][:8]} still in progress after ~{spec.duration_min * 2.6:.0f} min (normally ~{spec.duration_min:.0f})"


def _duplicates(est: Estate, site: str | None) -> str:
    s = _site(est, site)
    at, f = _finished_slot(est, s)
    r = est.emit(s, at, Outcome(duration_factor=f, duplicate_rate=0.09))
    return f"{s}: run {r['run_id'][:8]} wrote ~9% duplicate keys"


def _spike(est: Estate, site: str | None) -> str:
    s = _site(est, site)
    at, f = _finished_slot(est, s)
    r = est.emit(s, at, Outcome(duration_factor=f, row_factor=1.9))
    return f"{s}: run {r['run_id'][:8]} returned {r['rows']} rows (~90% above normal)"


def _schema_add(est: Estate, _site_name: str | None) -> str:
    est.alter_output(add="discount_pct")
    return f"{est.out_cfg.table}: column discount_pct added"


def _schema_drop(est: Estate, _site_name: str | None) -> str:
    col = next(iter(est.spec.extra_columns))
    est.alter_output(drop=col)
    return f"{est.out_cfg.table}: column {col} removed"


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario("incomplete", "Run succeeds but most rows are missing and a key field is empty (site layout change)", _incomplete),
        Scenario("recover", "A healthy re-run for the site; resolves its incident", _recover),
        Scenario("blocked", "Site answers HTTP 403 / captcha; pipeline 'succeeds' with zero rows", _blocked),
        Scenario("failed", "Scraper activity fails (HTTP 504 timeout); pipeline run Failed", _failed),
        Scenario("stuck", "Trigger row stays pending; ADF never picks it up", _stuck),
        Scenario("slow", "Run in progress far longer than normal", _slow),
        Scenario("duplicates", "Run writes the same keys several times", _duplicates),
        Scenario("spike", "Run returns far more rows than usual", _spike),
        Scenario("schema-add", "A new column appears in the staging table", _schema_add),
        Scenario("schema-drop", "A column disappears from the staging table", _schema_drop),
    )
}


def apply_scenario(cfg: ProjectConfig, name: str, site: str | None = None) -> str:
    if name not in SCENARIOS:
        raise KeyError(f"unknown scenario '{name}'; choose from {', '.join(SCENARIOS)}")
    return SCENARIOS[name].apply(Estate(cfg), site)
