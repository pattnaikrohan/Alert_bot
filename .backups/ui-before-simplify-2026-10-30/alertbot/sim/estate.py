"""Fake data estate for the prototype.

Builds, per project, a SQLite database that looks like the real thing: a
trigger table, scraped staging output, and simulated ADF pipeline + activity
run history (`_adf_pipeline_runs`, `_adf_activity_runs`) that the fake ADF
adapter serves through the same interface as the real REST adapter.

Table and column names come from the project config, so the bot reads this
estate exactly the way it will read Azure SQL. All values are fake.
"""

from __future__ import annotations

import json
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from ..config import OutputConfig, ProjectConfig
from ..connectors.sql_source import engine_for
from ..settings import get_settings
from ..timeutil import local_weekday, utcnow

# ── Estate specs ──────────────────────────────────────────────────────────────

WEEKDAY_FACTOR = [1.00, 0.95, 0.93, 0.94, 0.97, 0.72, 0.64]  # Mon..Sun


@dataclass(frozen=True)
class SiteSpec:
    name: str
    code: str
    base_rows: int  # typical Monday volume per run
    duration_min: float  # typical pipeline duration
    slots: tuple[tuple[int, int], ...]  # (hour, minute) UTC request times each day


@dataclass(frozen=True)
class EstateSpec:
    sites: tuple[SiteSpec, ...]
    extra_columns: dict[str, sa.types.TypeEngine]
    row_factory: Callable[[SiteSpec, int, random.Random], dict[str, Any]]
    normal_null_rates: dict[str, float]
    scraper_function: str


_ADJ = ["Compact", "Modern", "Classic", "Spacious", "Bright", "Renovated", "Premium", "Budget", "Vintage", "Smart"]
_NOUN = ["Apartment", "Studio", "Hatchback", "Sofa", "Road bike", "Laptop", "Office desk", "Townhouse", "Camera", "Coupé"]
_CITY = ["Leeds", "Lyon", "Porto", "Gdansk", "Utrecht", "Malmö", "Turin", "Graz", "Cork", "Ghent"]
_BRAND = ["Northwind", "Fabrikam", "Contoso", "Litware", "Tailspin", "Adatum", "Proseware", "Wingtip"]
_PRODUCT = ["Kettle", "Blender", "Air fryer", "Headphones", "Monitor 27in", "Desk lamp", "Router", "Vacuum", "Toaster"]


def _slipstream_row(site: SiteSpec, n: int, rng: random.Random) -> dict[str, Any]:
    lid = f"{site.code}{n:06d}"
    return {
        "listing_id": lid,
        "title": f"{rng.choice(_ADJ)} {rng.choice(_NOUN).lower()} in {rng.choice(_CITY)}",
        "price": round(rng.lognormvariate(5.6, 0.8), 2),
        "currency": "EUR",
        "url": f"https://{site.code.lower()}.example.com/listing/{lid}",
        "location": rng.choice(_CITY),
    }


def _csdw_row(site: SiteSpec, n: int, rng: random.Random) -> dict[str, Any]:
    sku = f"{site.code}-{n:05d}"
    return {
        "sku": sku,
        "product_name": f"{rng.choice(_BRAND)} {rng.choice(_PRODUCT)}",
        "brand": rng.choice(_BRAND),
        "price": round(rng.uniform(9, 420), 2),
        "availability": rng.choices(["in_stock", "low_stock", "out_of_stock"], [0.8, 0.12, 0.08])[0],
        "product_url": f"https://shop-{site.code.lower()}.example.com/p/{sku}",
    }


SPECS: dict[str, EstateSpec] = {
    "slipstream": EstateSpec(
        sites=(
            SiteSpec("Site A", "SA", 1050, 22, ((5, 30),)),
            SiteSpec("Site B", "SB", 640, 14, ((5, 45),)),
            SiteSpec("Site C", "SC", 880, 18, ((6, 0),)),
            SiteSpec("Site D", "SD", 420, 11, ((6, 15),)),
            SiteSpec("Site E", "SE", 300, 9, ((6, 30),)),
        ),
        extra_columns={"currency": sa.String(3), "location": sa.String(80)},
        row_factory=_slipstream_row,
        normal_null_rates={"title": 0.001, "price": 0.004, "url": 0.0},
        scraper_function="func-slipstream-scraper",
    ),
    "csdw": EstateSpec(
        sites=(
            SiteSpec("Retailer North", "RN", 380, 9, ((0, 0), (8, 0), (16, 0))),
            SiteSpec("Retailer South", "RS", 520, 12, ((0, 10), (8, 10), (16, 10))),
            SiteSpec("Retailer East", "RE", 290, 7, ((0, 20), (8, 20), (16, 20))),
            SiteSpec("Retailer West", "RW", 450, 10, ((0, 30), (8, 30), (16, 30))),
        ),
        extra_columns={"brand": sa.String(80)},
        row_factory=_csdw_row,
        normal_null_rates={"product_name": 0.001, "price": 0.006, "availability": 0.002},
        scraper_function="func-csdw-scraper",
    ),
}


# ── Schema ────────────────────────────────────────────────────────────────────


def _split(name: str) -> tuple[str | None, str]:
    schema, _, table = name.rpartition(".")
    return (schema or None), table


def _tables(cfg: ProjectConfig, spec: EstateSpec) -> tuple[sa.MetaData, sa.Table, sa.Table, sa.Table, sa.Table]:
    md = sa.MetaData()
    tt = cfg.trigger_table
    schema, name = _split(tt.table)
    trigger = sa.Table(
        name, md,
        sa.Column(tt.id_column, sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(tt.site_column, sa.String(120), nullable=False),
        sa.Column(tt.status_column, sa.String(32), nullable=False),
        sa.Column(tt.run_id_column, sa.String(64)),
        sa.Column(tt.created_column, sa.DateTime, nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime),
        schema=schema,
    )
    out = cfg.outputs[0]
    schema, name = _split(out.table)
    key_cols = [k for k in out.key if k != out.group_by]
    cols = [sa.Column(out.group_by, sa.String(120), nullable=False)]
    cols += [sa.Column(k, sa.String(64), nullable=False) for k in key_cols]
    for f in out.required_fields:
        if f not in key_cols and f != out.group_by:
            cols.append(sa.Column(f, sa.Float if f == "price" else sa.String(300)))
    cols += [sa.Column(c, t) for c, t in spec.extra_columns.items()]
    cols += [sa.Column("scraped_at", sa.DateTime), sa.Column(out.loaded_at, sa.DateTime, nullable=False)]
    output = sa.Table(name, md, *cols, sa.Index(f"ix_{name}_site_loaded", out.group_by, out.loaded_at), schema=schema)

    runs = sa.Table(
        "_adf_pipeline_runs", md,
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("pipeline_name", sa.String(120)),
        sa.Column("status", sa.String(24)),
        sa.Column("run_start", sa.DateTime),
        sa.Column("run_end", sa.DateTime),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("parameters", sa.Text),
        sa.Column("message", sa.Text),
    )
    acts = sa.Table(
        "_adf_activity_runs", md,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), index=True),
        sa.Column("activity_name", sa.String(120)),
        sa.Column("activity_type", sa.String(60)),
        sa.Column("status", sa.String(24)),
        sa.Column("start", sa.DateTime),
        sa.Column("end", sa.DateTime),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("output", sa.Text),
        sa.Column("error", sa.Text),
    )
    return md, trigger, output, runs, acts


# ── One run ───────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    """What should happen to one simulated scrape."""

    kind: str = "ok"  # ok | failed | running | stuck | blocked
    row_factor: float = 1.0
    duration_factor: float = 1.0
    null_overrides: dict[str, float] = field(default_factory=dict)
    duplicate_rate: float = 0.001
    http_status: int = 200
    error: str = ""
    selector_misses: int = 0


class Estate:
    def __init__(self, cfg: ProjectConfig, seed: int = 7):
        if cfg.project not in SPECS:
            raise KeyError(f"no fake estate spec for project '{cfg.project}'")
        if cfg.pipeline.type != "fake_adf":
            raise ValueError(f"{cfg.project}: refusing to write a fake estate into a non-fake pipeline config")
        self.cfg = cfg
        self.spec = SPECS[cfg.project]
        src = cfg.source(cfg.trigger_table.source)
        if src.type != "sqlite":
            raise ValueError(f"{cfg.project}: the simulator only writes to sqlite sources, never to {src.type}")
        self.engine = engine_for(src)
        self.md, self.trigger, self.output, self.runs, self.acts = _tables(cfg, self.spec)
        self.out_cfg: OutputConfig = cfg.outputs[0]
        self.rng = random.Random(f"{cfg.project}:{seed}")
        self.tz = get_settings().tz
        self._status = {s: getattr(cfg.trigger_table.statuses, s)[0] for s in ("pending", "running", "succeeded", "failed")}

    # lifecycle ---------------------------------------------------------------

    def reset(self) -> None:
        url = self.cfg.source(self.cfg.trigger_table.source).url or ""
        path = Path(url.split("///", 1)[-1])
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        self.md.create_all(self.engine)

    def site(self, name: str) -> SiteSpec:
        for s in self.spec.sites:
            if s.name.lower() == name.lower() or s.code.lower() == name.lower():
                return s
        raise KeyError(f"unknown site '{name}'; known: {[s.name for s in self.spec.sites]}")

    def generate_history(self, days: int = 56, now: datetime | None = None) -> int:
        """Normal runs for the past `days` days plus today's runs that would already have finished."""
        now = now or utcnow()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        count = 0
        with self.engine.begin() as conn:
            for d in range(days, -1, -1):
                day = today - timedelta(days=d)
                for site in self.spec.sites:
                    for hour, minute in site.slots:
                        requested = day + timedelta(hours=hour, minutes=minute)
                        finish = requested + timedelta(minutes=site.duration_min * 1.6 + 5)
                        if finish > now - timedelta(minutes=5):
                            continue
                        outcome = Outcome()
                        # The odd transient failure in older history; the last three days stay clean
                        # so the demo starts green and every incident comes from a chosen scenario.
                        if d >= 3 and self.rng.random() < 0.012:
                            outcome = Outcome(kind="failed", error="Scraper timed out after 300 seconds", http_status=504)
                        self._emit(conn, site, requested, outcome, now=now)
                        count += 1
        return count

    def catch_up(self, now: datetime | None = None) -> int:
        """Keep the fake estate alive: emit every normal scheduled scrape that has finished since each site's
        latest trigger row. Idempotent, so the worker can call it every tick; a scenario run stays the latest
        until the site's next slot comes round, which then "recovers" it like a real re-run would."""
        now = now or utcnow()
        tt = self.cfg.trigger_table
        with self.engine.connect() as conn:
            latest = dict(conn.execute(
                sa.select(self.trigger.c[tt.site_column], sa.func.max(self.trigger.c[tt.created_column])).group_by(self.trigger.c[tt.site_column])
            ).all())
        count = 0
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.engine.begin() as conn:
            for d in (2, 1, 0):
                day = today - timedelta(days=d)
                for site in self.spec.sites:
                    last = latest.get(site.name)
                    if isinstance(last, str):
                        last = datetime.fromisoformat(last)
                    for hour, minute in site.slots:
                        requested = day + timedelta(hours=hour, minutes=minute)
                        if last is not None and requested <= last:
                            continue
                        if requested + timedelta(minutes=site.duration_min * 1.6 + 5) > now - timedelta(minutes=2):
                            continue
                        self._emit(conn, site, requested, Outcome(), now=now)
                        latest[site.name] = requested
                        count += 1
        return count

    # the core writer ---------------------------------------------------------

    def emit(self, site_name: str, requested_at: datetime, outcome: Outcome) -> dict[str, Any]:
        with self.engine.begin() as conn:
            return self._emit(conn, self.site(site_name), requested_at, outcome, now=utcnow())

    def _emit(self, conn: sa.Connection, site: SiteSpec, requested: datetime, o: Outcome, now: datetime) -> dict[str, Any]:
        tt = self.cfg.trigger_table
        rng = self.rng
        pipeline_name = (self.cfg.pipeline.pipelines or ["pl_scrape"])[0]

        if o.kind == "stuck":
            res = conn.execute(
                self.trigger.insert().values(
                    {tt.site_column: site.name, tt.status_column: self._status["pending"], tt.run_id_column: None,
                     tt.created_column: requested, "updated_at": requested}
                )
            )
            return {"trigger_id": res.inserted_primary_key[0], "run_id": None, "rows": 0}

        run_id = str(uuid.uuid4())  # unique across processes (row data stays seeded)
        start = requested + timedelta(seconds=rng.randint(20, 75))
        weekday = local_weekday(requested, self.tz)
        duration = site.duration_min * rng.uniform(0.85, 1.15) * o.duration_factor * 60
        if o.kind == "failed":
            duration *= rng.uniform(0.3, 0.6)
        end = start + timedelta(seconds=duration)
        running = o.kind == "running"
        if running:
            end = None

        expected_rows = site.base_rows * WEEKDAY_FACTOR[weekday] * rng.uniform(0.96, 1.04)
        n_rows = 0
        if o.kind in ("ok",):
            n_rows = max(0, int(expected_rows * o.row_factor))
        pages = max(1, int(expected_rows / 24))

        trigger_state = {"ok": "succeeded", "blocked": "succeeded", "failed": "failed", "running": "running"}[o.kind]
        trig = conn.execute(
            self.trigger.insert().values(
                {tt.site_column: site.name, tt.status_column: self._status[trigger_state], tt.run_id_column: run_id,
                 tt.created_column: requested, "updated_at": end or now}
            )
        )
        trigger_id = trig.inserted_primary_key[0]

        status = {"ok": "Succeeded", "blocked": "Succeeded", "failed": "Failed", "running": "InProgress"}[o.kind]
        conn.execute(
            self.runs.insert().values(
                run_id=run_id, pipeline_name=pipeline_name, status=status, run_start=start, run_end=end,
                duration_ms=None if running else int(duration * 1000),
                parameters=json.dumps({"site": site.name, "request_id": trigger_id}),
                message=(f"Operation on target Execute scraper failed: {o.error}" if o.kind == "failed" else ""),
            )
        )

        # Activities: Lookup → Execute scraper (Azure Function) → Copy to staging → Update trigger status
        t = start
        acts: list[dict[str, Any]] = []

        def act(name: str, typ: str, secs: float, status: str, output: dict | None, error: dict | None = None) -> None:
            nonlocal t
            a_end = None if status == "InProgress" else t + timedelta(seconds=secs)
            acts.append(dict(run_id=run_id, activity_name=name, activity_type=typ, status=status, start=t, end=a_end,
                             duration_ms=None if a_end is None else int(secs * 1000),
                             output=json.dumps(output) if output is not None else None,
                             error=json.dumps(error) if error else None))
            t = a_end or t

        act("Lookup site config", "Lookup", 4, "Succeeded", {"firstRow": {"site": site.name, "max_pages": pages + 40}})
        scrape_secs = duration * 0.86
        if o.kind == "failed":
            act("Execute scraper", "AzureFunctionActivity", scrape_secs, "Failed", None,
                {"errorCode": "3608", "message": f"Call to provided Azure function '{self.spec.scraper_function}' failed "
                 f"with status-'{o.http_status}' and message - '{o.error}'", "failureType": "UserError",
                 "target": "Execute scraper"})
        elif running:
            act("Execute scraper", "AzureFunctionActivity", 0, "InProgress", None)
        else:
            items = 0 if o.kind == "blocked" else n_rows
            act("Execute scraper", "AzureFunctionActivity", scrape_secs, "Succeeded",
                {"statusCode": o.http_status, "pagesFetched": 0 if o.kind == "blocked" else pages,
                 "itemsParsed": items, "selectorMisses": o.selector_misses,
                 "blockedReason": "HTTP 403 from origin; captcha challenge served" if o.kind == "blocked" else None,
                 "function": self.spec.scraper_function})
            act("Copy to staging", "Copy", duration * 0.1, "Succeeded",
                {"rowsRead": n_rows, "rowsCopied": n_rows, "dataWritten": n_rows * 412, "copyDuration": int(duration * 0.1),
                 "sinkTable": self.out_cfg.table})
            act("Update trigger status", "SqlServerStoredProcedure", 3, "Succeeded",
                {"effectiveIntegrationRuntime": "AutoResolveIntegrationRuntime"})
        conn.execute(self.acts.insert(), acts)

        if n_rows and end is not None:
            self._write_rows(conn, site, n_rows, end, o)
        return {"trigger_id": trigger_id, "run_id": run_id, "rows": n_rows, "status": status}

    def _write_rows(self, conn: sa.Connection, site: SiteSpec, n: int, end: datetime, o: Outcome) -> None:
        out = self.out_cfg
        rng = self.rng
        null_rates = {**self.spec.normal_null_rates, **o.null_overrides}
        # Write only columns the table still has (a schema scenario may have dropped one).
        schema, name = _split(out.table)
        live = {c["name"] for c in sa.inspect(conn).get_columns(name, schema=schema)}
        rows: list[dict[str, Any]] = []
        dupes = int(n * o.duplicate_rate)
        for i in range(n):
            n_id = rng.randrange(0, n) if i >= n - dupes else i
            row = self.spec.row_factory(site, n_id, rng)
            for f, rate in null_rates.items():
                if rate and rng.random() < rate:
                    row[f] = None
            row[out.group_by] = site.name
            row["scraped_at"] = end - timedelta(seconds=rng.randint(60, 900))
            row[out.loaded_at] = end - timedelta(seconds=rng.randint(0, 45))
            rows.append({k: v for k, v in row.items() if k in live})
        conn.execute(self.output.insert(), rows)

    # schema changes ------------------------------------------------------------

    def alter_output(self, add: str | None = None, drop: str | None = None) -> None:
        schema, name = _split(self.out_cfg.table)
        qualified = f'"{schema}"."{name}"' if schema else f'"{name}"'
        with self.engine.begin() as conn:
            if add:
                conn.exec_driver_sql(f'ALTER TABLE {qualified} ADD COLUMN "{add}" VARCHAR(100)')
            if drop:
                conn.exec_driver_sql(f'ALTER TABLE {qualified} DROP COLUMN "{drop}"')
