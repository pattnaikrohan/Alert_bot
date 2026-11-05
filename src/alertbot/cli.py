"""`alertbot` command line.

    alertbot demo                          build fake estates, learn baselines, play the doc's story
    alertbot scenario slipstream incomplete inject trouble (or `recover`) and run a cycle
    alertbot cycle                         run one cycle for every project
    alertbot status                        health, incidents, notifier state per project
    alertbot notify-test slipstream        send a test card to the project's Power Automate flow
    alertbot serve                         start the API (portal talks to it)
    alertbot worker                        run cycles on each project's schedule
"""

from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich import box
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import func, select

from .config import load_ai_config, load_projects
from .db import (
    BotCycle,
    CheckResult,
    Incident,
    Notification,
    Project,
    User,
    UserProject,
    create_all,
    session_scope,
)
from .secrets import describe_secret
from .settings import get_settings
from .timeutil import utcnow

app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich",
                  help="Alert Bot: AI data reliability bot for scraping pipelines.")
members_app = typer.Typer(no_args_is_help=True, help="Manage who can see which project.")
app.add_typer(members_app, name="members")
# Windows consoles default to a legacy code page; print UTF-8 (arrows, box lines) instead of crashing on them.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() not in ("utf-8", "utf8"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
console = Console()

SEV_STYLE = {"critical": "bold red", "warning": "yellow", "info": "cyan"}


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s", datefmt="%H:%M:%S",
                        handlers=[RichHandler(console=console, show_path=False, markup=False)], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for noisy in ("azure", "msal", "urllib3"):  # Azure SDK logs every HTTP request at INFO
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False) -> None:
    _setup_logging(verbose)


def _project(key: str):  # noqa: ANN202
    projects = load_projects()
    if key not in projects:
        raise typer.BadParameter(f"unknown project '{key}'; known: {', '.join(projects)}")
    return projects[key]


def _selected(project: str | None):  # noqa: ANN202
    return [_project(project)] if project else list(load_projects().values())


def _print_reports(reports) -> None:  # noqa: ANN001
    t = Table(box=box.SIMPLE_HEAVY, title="Cycle", title_justify="left")
    for col in ("Project", "Status", "Runs", "Measured", "Checks", "Failing", "Opened", "Worse", "Resolved", "Alerts", "Time"):
        t.add_column(col, justify="right" if col not in ("Project", "Status", "Alerts") else "left")
    for r in reports:
        alerts = ", ".join(f"{v} {k.replace('_', '-')}" for k, v in r.notifications.items() if v) or "-"
        status = {"ok": "[green]ok[/]", "error": f"[red]error[/] {r.error}", "skipped": f"[yellow]skipped[/] {r.error}"}[r.status]
        t.add_row(r.project, status, str(r.runs), str(r.measured), str(r.checks),
                  f"[red]{r.failed_checks}[/]" if r.failed_checks else "0",
                  str(r.changes.opened), str(r.changes.worsened), str(r.changes.resolved), alerts, f"{r.seconds}s")
    console.print(t)


# ── setup ─────────────────────────────────────────────────────────────────────


@app.command("init-db")
def init_db() -> None:
    """Create the metadata DB tables (idempotent)."""
    create_all()
    console.print(f"[green]✓[/] metadata DB ready at [bold]{get_settings().db_url}[/]")


@app.command()
def seed() -> None:
    """Upsert projects from config/ and people from config/members.yaml."""
    from .seed import seed as do_seed

    create_all()
    with session_scope() as s:
        p, u = do_seed(s)
    console.print(f"[green]✓[/] {p} projects, {u} people seeded")


@app.command()
def demo(
    days: Annotated[int, typer.Option(help="Days of fake history to generate.")] = 56,
    story: Annotated[bool, typer.Option(help="Play the doc's bad-run story after setup.")] = True,
) -> None:
    """Reset everything to a fresh fake estate with learned baselines, then play the story."""
    from .db.session import get_engine
    from .engine.cycle import backfill, run_cycle
    from .seed import seed as do_seed
    from .sim.estate import Estate
    from .sim.scenarios import apply_scenario

    settings = get_settings()
    console.print(Panel.fit("[bold]Alert Bot · prototype demo[/]\nAll data below is fake.", border_style="cyan"))

    if settings.db_url.startswith("sqlite"):
        get_engine().dispose()
        path = Path(settings.db_url.split("///", 1)[-1])
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
    create_all()

    projects = list(load_projects().values())
    with console.status("Generating fake estates…"):
        for cfg in projects:
            est = Estate(cfg)
            est.reset()
            n = est.generate_history(days=days)
            console.print(f"  [green]✓[/] {cfg.display_name}: {n:,} historical scrapes over {days} days")

    with session_scope() as s:
        p, u = do_seed(s)
    console.print(f"  [green]✓[/] {p} projects, {u} people")

    with console.status("Learning baselines from history…"):
        for cfg in projects:
            n = backfill(cfg, days=days + 2)
            console.print(f"  [green]✓[/] {cfg.display_name}: measured {n:,} runs")

    console.print("\n[bold]First cycle[/] (everything should be healthy)")
    _print_reports([run_cycle(cfg) for cfg in projects])

    if story and "slipstream" in load_projects():
        console.print("\n[bold]The story:[/] Slipstream Site A comes back with most rows missing and prices empty")
        console.print("  → " + apply_scenario(_project("slipstream"), "incomplete"))
        _print_reports([run_cycle(cfg) for cfg in projects])
        _incident_table(None)

    console.print(
        "\n[bold]Next[/]\n"
        "  [cyan]alertbot serve[/]                      API on http://localhost:8000\n"
        "  [cyan]cd portal; npm run dev[/]             portal on http://localhost:5173\n"
        "  [cyan]alertbot scenario csdw blocked[/]     more trouble · [cyan]alertbot scenario --list[/]\n"
        "  [cyan]alertbot scenario slipstream recover[/] resolve the incident"
    )


@app.command()
def scenario(
    project: Annotated[str | None, typer.Argument(help="Project key.")] = None,
    name: Annotated[str | None, typer.Argument(help="Scenario name.")] = None,
    site: Annotated[str | None, typer.Option(help="Site (defaults to the project's demo site).")] = None,
    run: Annotated[bool, typer.Option(help="Run a cycle for the project afterwards.")] = True,
    list_: Annotated[bool, typer.Option("--list", help="List scenarios.")] = False,
) -> None:
    """Inject a scenario into a project's fake estate."""
    from .engine.cycle import run_cycle
    from .sim.scenarios import SCENARIOS, apply_scenario

    if list_ or not (project and name):
        t = Table(box=box.SIMPLE, title="Scenarios", title_justify="left")
        t.add_column("Name", style="cyan")
        t.add_column("What happens")
        for s in SCENARIOS.values():
            t.add_row(s.name, s.description)
        console.print(t)
        console.print("Usage: [cyan]alertbot scenario <project> <name> [--site NAME][/]")
        return
    cfg = _project(project)
    console.print("→ " + apply_scenario(cfg, name, site))
    if run:
        _print_reports([run_cycle(cfg)])
        _incident_table(project)


# ── running ───────────────────────────────────────────────────────────────────


@app.command()
def backfill(
    project: Annotated[str | None, typer.Argument()] = None,
    days: Annotated[int, typer.Option(help="How far back to measure.")] = 60,
) -> None:
    """Measure historical runs so baselines exist from day one (never alerts)."""
    from .engine.cycle import backfill as do_backfill

    create_all()
    for cfg in _selected(project):
        console.print(f"[green]✓[/] {cfg.display_name}: measured {do_backfill(cfg, days):,} runs")


@app.command()
def cycle(
    project: Annotated[str | None, typer.Argument()] = None,
    alert: Annotated[bool, typer.Option(help="Queue alerts for incident changes.")] = True,
) -> None:
    """Run one cycle now (ignores schedules)."""
    from .engine.cycle import run_cycle

    create_all()
    _print_reports([run_cycle(cfg, alert=alert) for cfg in _selected(project)])


@app.command()
def worker(
    tick: Annotated[int, typer.Option(help="Seconds between looks at Azure for new runs (and schedule checks).")] = 15,
    simulate: Annotated[bool, typer.Option(help="Prototype: keep the fake estates producing their scheduled scrapes.")] = False,
) -> None:
    """Run continuously: watch Azure for new or changed runs (checked at once), and run each project's scheduled round."""
    from .engine.cycle import run_due
    from .engine.watch import watch_all

    create_all()
    estates = []
    if simulate:
        from .sim.estate import Estate

        estates = [Estate(cfg) for cfg in load_projects().values() if cfg.pipeline and cfg.pipeline.type == "fake_adf"]
    console.print(f"[bold]Worker started[/] · watching Azure for new runs · checking schedules every {tick}s"
                  f"{' · simulating ' + ', '.join(e.cfg.project for e in estates) if estates else ''} · Ctrl+C to stop")
    try:
        while True:
            try:
                for est in estates:
                    n = est.catch_up()
                    if n:
                        console.print(f"[dim]{est.cfg.project}: {n} scheduled scrape(s) simulated[/]")
                for key, changes, report in watch_all():
                    what = ", ".join(f"{c.pipeline} {c.status.lower()}" for c in changes[:3]) + (f" +{len(changes) - 3}" if len(changes) > 3 else "")
                    console.print(f"[cyan]{key}[/]: {what} → checked ({report.status}, {report.changes.opened} opened, {report.changes.resolved} resolved)")
                reports = run_due()
                if reports:
                    _print_reports(reports)
                from .discovery import rediscover_all
                from .engine.report import due_reports

                rediscover_all()  # every few hours: what each person can see in Azure (new resources, revoked access)
                from .dq.storage import scan_all

                scan_all()  # hourly: each connected database's table sizes and usage (kept, so a restart doesn't erase it)
                try:  # its own guard, like the next one
                    from .dq.compute import scan_all as compute_scan_all

                    compute_scan_all()  # Snowflake compute: hourly, what's running every 10 minutes; new alerts to Teams
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).exception("compute cost scan failed this round")
                try:  # Snowflake Cortex AI usage: hourly, its own guard
                    from .dq.cortex import scan_all as cortex_scan_all

                    cortex_scan_all()
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).exception("Cortex AI usage scan failed this round")
                from .dq.suspicious import scan_due

                try:  # its own guard: a problem here must never hold up the morning report below
                    scan_due()  # each checked table: rules from its column names (once), and rows breaking them, on its own schedule
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).exception("suspicious-value checks failed this round")

                for key in due_reports():
                    console.print(f"[dim]{key}: morning report sent[/]")
            except Exception:  # noqa: BLE001 - one bad round (e.g. a locked database) must not stop the worker
                logging.getLogger(__name__).exception("worker round failed; trying again in %ss", tick)
            time.sleep(tick)
    except KeyboardInterrupt:
        console.print("stopped")


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes.")] = False,
) -> None:
    """Start the API that the portal talks to."""
    import uvicorn

    create_all()
    uvicorn.run("alertbot.api.main:app", host=host, port=port, reload=reload, log_level="info")


@app.command()
def start(
    host: str = "127.0.0.1",
    port: int = 8000,
    tick: Annotated[int, typer.Option(help="Seconds between the worker's looks at Azure.")] = 15,
) -> None:
    """Start everything in one go: the worker (watching Azure, 'Live') and the portal's server. Ctrl+C stops both."""
    import subprocess

    import uvicorn

    create_all()
    # the worker runs as its own process, so a slow Azure round never holds up the website
    w = subprocess.Popen([sys.executable, "-c", "from alertbot.cli import app; app()", "worker", "--tick", str(tick)])
    console.print(f"[bold]Alert Bot starting[/] · portal on http://{host}:{port} · worker running · Ctrl+C stops both")
    try:
        uvicorn.run("alertbot.api.main:app", host=host, port=port, log_level="info")
    finally:
        w.terminate()
        try:
            w.wait(timeout=10)
        except subprocess.TimeoutExpired:
            w.kill()
        console.print("stopped: server and worker")


# ── looking ───────────────────────────────────────────────────────────────────


def _incident_table(project: str | None) -> None:
    with session_scope() as s:
        q = (select(Incident, Project.key).join(Project, Project.id == Incident.project_id)
             .order_by(Incident.opened_at.desc()).limit(12))
        if project:
            q = q.where(Project.key == project)
        rows = s.execute(q).all()
        t = Table(box=box.SIMPLE_HEAVY, title="Incidents", title_justify="left")
        for col in ("#", "Project", "Severity", "Status", "Title", "Likely cause"):
            t.add_column(col, overflow="fold")
        for inc, key in rows:
            status = {"open": "[red]open[/]", "acknowledged": "[yellow]ack[/]", "resolved": "[green]resolved[/]"}[inc.status]
            t.add_row(str(inc.id), key, f"[{SEV_STYLE[inc.severity]}]{inc.severity}[/]", status, inc.title,
                      inc.likely_cause[:110] + ("…" if len(inc.likely_cause) > 110 else ""))
    console.print(t if rows else "[dim]No incidents.[/]")


@app.command()
def incidents(project: Annotated[str | None, typer.Argument()] = None) -> None:
    """List recent incidents."""
    _incident_table(project)


@app.command()
def status() -> None:
    """Health, incidents and alert delivery state for each project."""
    create_all()
    ai = load_ai_config()
    t = Table(box=box.SIMPLE_HEAVY, title="Projects", title_justify="left")
    for col in ("Project", "Health", "Open", "Last cycle", "Alerts via", "Flow URL", "Delivered"):
        t.add_column(col)
    with session_scope() as s:
        for cfg in load_projects().values():
            p = s.scalars(select(Project).where(Project.key == cfg.project)).first()
            if p is None:
                t.add_row(cfg.display_name, "-", "-", "never", cfg.alerts.notifier, describe_secret(cfg.alerts.flow_url), "-")
                continue
            last = s.scalars(select(BotCycle).where(BotCycle.project_id == p.id).order_by(BotCycle.started_at.desc()).limit(1)).first()
            health = "-"
            if last and last.status == "ok" and last.checks_run:
                pct = 100 * (last.checks_run - last.checks_failed) / last.checks_run
                health = f"[{'green' if pct >= 95 else 'yellow' if pct >= 80 else 'red'}]{pct:.0f}%[/]"
            open_n = s.scalar(select(func.count()).select_from(Incident).where(Incident.project_id == p.id, Incident.status != "resolved"))
            notes = dict(s.execute(select(Notification.status, func.count()).where(Notification.project_id == p.id).group_by(Notification.status)).all())
            ago = f"{(utcnow() - last.started_at).total_seconds() / 60:.0f} min ago ({last.status})" if last else "never"
            flow = describe_secret(cfg.alerts.flow_url)
            flow = flow if "(set)" in flow else f"[yellow]{flow} → dry-run[/]"
            t.add_row(cfg.display_name, health, f"[red]{open_n}[/]" if open_n else "0", ago, cfg.alerts.notifier, flow,
                      ", ".join(f"{v} {k}" for k, v in notes.items()) or "-")
    console.print(t)
    console.print(f"AI: {'[green]enabled[/] ' + ai.provider + ': ' + ' → '.join(ai.chat_models) if ai.ready else '[dim]off (rule-based analysis)[/]'}"
                  f" · DB: {get_settings().db_url} · auth: {get_settings().auth_mode}")


@app.command("notify-test")
def notify_test(project: Annotated[str, typer.Argument()]) -> None:
    """Send a clearly-labelled test alert through the project's Power Automate flow."""
    from .connectors.notifiers import build_notifier
    from .engine.notify import adaptive_card

    cfg = _project(project)
    notifier = build_notifier(cfg.alerts)
    payload = {
        "schema_version": "1.0", "event": "test", "occurred_at": utcnow().isoformat() + "Z",
        "project": cfg.project, "project_name": cfg.display_name, "incident_id": 0, "severity": "info",
        "status": "open", "title": "Test alert from Alert Bot", "summary": "If you can read this, the flow works.",
        "impact": "None. This is a test.", "likely_cause": "Someone ran `alertbot notify-test`.",
        "action": "Nothing to do.", "confidence": "high", "analysis_source": "rules", "checks": [],
        "scope": "test:connection", "portal_url": f"{get_settings().portal_url}/p/{cfg.project}",
        "teams_channel": cfg.alerts.teams_channel, "email_owners": False, "owner_emails": [],
    }
    payload["card"] = adaptive_card(payload)
    if not notifier.configured:
        console.print(Panel(
            f"No flow URL for [bold]{cfg.display_name}[/] ({describe_secret(cfg.alerts.flow_url)}).\n\n"
            f"Add it to [cyan].env[/]:  [bold]{cfg.alerts.flow_url.removeprefix('env://') or 'PA_' + cfg.project.upper() + '_URL'}=https://…[/]\n"
            "or point [cyan]alerts.flow_url[/] at a Key Vault secret ([bold]kv://name[/]). Then run this again.",
            title="Dry-run mode", border_style="yellow"))
        return
    notifier.send(payload, f"test-{int(time.time())}")
    console.print(f"[green]✓[/] test alert sent via {notifier.channel} for {cfg.display_name}")


@app.command("config-check")
def config_check() -> None:
    """Validate every config file and show what each secret reference resolves to (never the value)."""
    projects = load_projects()
    for cfg in projects.values():
        what = (f"{len(cfg.feeds)} feed(s) over {len(cfg.schema_tables)} table(s)" if cfg.kind == "feeds"
                else f"{len(cfg.outputs)} output(s), pipeline {cfg.pipeline.type if cfg.pipeline else '-'}")
        console.print(f"[green]✓[/] {cfg.project}: {what}, "
                      f"alerts {cfg.alerts.notifier} · flow URL {describe_secret(cfg.alerts.flow_url)}")
        for s in cfg.sources:
            if s.type == "snowflake":
                console.print(f"    source {s.id}: snowflake account {describe_secret(s.account)} user {describe_secret(s.user)} "
                              f"role {s.role or '-'} warehouse {s.warehouse or '-'} auth {s.authenticator}")
        if (q := cfg.quality) is not None and q.enabled:
            console.print(f"    data quality: {q.source.type} account {describe_secret(q.source.account)} user {describe_secret(q.source.user)} "
                          f"auth {q.source.authenticator} · {'every table the pipelines load' if q.auto else 'listed tables'} "
                          f"in {', '.join(q.environments) or 'all environments'} · {len(q.tables)} table setting(s)")
        if cfg.alerts.morning_report:
            console.print(f"    morning report: daily at {cfg.alerts.morning_report}")
    ai = load_ai_config()
    console.print(f"[green]✓[/] ai.yaml: enabled={ai.enabled} provider={ai.provider or '-'} models={' → '.join(ai.chat_models) or '-'} "
                  f"key {describe_secret(ai.api_key)}")


@app.command("feeds-check")
def feeds_check(
    project: Annotated[str | None, typer.Argument(help="Project key; default: every feeds project")] = None,
    days: Annotated[int, typer.Option(help="How far back to look")] = 7,
) -> None:
    """Connect to each feed's source (read-only) and show its latest row and recent volume. Never alerts."""
    from datetime import timedelta

    from .connectors.feeds import FeedProducers, FeedSource, ProducerUnavailable

    now = utcnow()
    for cfg in load_projects().values():
        if cfg.kind != "feeds" or (project and cfg.project != project):
            continue
        sources = {s.id: FeedSource(s) for s in cfg.sources}
        producers = FeedProducers(cfg)
        table = Table(title=f"{cfg.display_name} feeds", box=box.SIMPLE_HEAVY)
        for col in ("Feed", "Table", "Latest row (UTC)", "Rows 24h", f"Rows {days}d", "Producer"):
            table.add_column(col)
        for f in cfg.feeds:
            src = sources[f.source]
            try:
                last = src.last_loaded(f, now - timedelta(days=days))
                n24 = src.window_metrics(f, now - timedelta(hours=24), now).row_count
                nd = src.window_metrics(f, now - timedelta(days=days), now).row_count
                row = [f"{last:%Y-%m-%d %H:%M}" if last else "[yellow]none[/]", f"{n24:,}", f"{nd:,}"]
            except Exception as exc:  # noqa: BLE001 - show the error per feed
                row = [f"[red]{type(exc).__name__}: {str(exc)[:80]}[/]", "-", "-"]
            try:
                ev = producers.events(f, now - timedelta(days=1))
                bad = sum(e.status.upper() == "FAILED" for e in ev)
                prod = f"{len(ev)} event(s), {bad} failed" if ev else f"{f.producer.type} (no telemetry)"
            except ProducerUnavailable as exc:
                prod = f"[red]{str(exc)[:60]}[/]"
            table.add_row(f.name, f.table, *row, prod)
        console.print(table)


# ── Azure Data Factory ────────────────────────────────────────────────────────


def _adf_project(project: str | None):  # noqa: ANN202
    adf = [c for c in load_projects().values() if c.kind == "adf" and (project is None or c.project == project)]
    if not adf:
        raise typer.BadParameter(f"no Data Factory project{f' called {project}' if project else ''} in {get_settings().config_dir}")
    return adf


@app.command("adf-login")
def adf_login(project: Annotated[str | None, typer.Argument(help="Data Factory project to check afterwards")] = None) -> None:
    """Connect the bot to Azure from a terminal (an admin signing in to the portal does the same)."""
    from .msauth import terminal_sign_in

    cfg = _adf_project(project)[0]
    assert cfg.pipeline is not None

    def show(uri: str, code: str, expires) -> None:  # noqa: ANN001
        console.print(Panel.fit(f"Open [bold]{uri}[/] and enter the code\n\n      [bold cyan]{code}[/]\n\n"
                                f"Sign in with an account that can read [bold]{cfg.pipeline.factory}[/] "
                                f"(expires {expires:%H:%M} UTC).", title="Connect the alert bot to Azure", border_style="cyan"))
        sys.stdout.flush()

    who = terminal_sign_in(show)
    console.print(f"[green]✓[/] Connected as [bold]{who['email']}[/]. Checking the factory…")
    sys.stdout.flush()
    adf_check(cfg.project, days=3)


@app.command("adf-logout")
def adf_logout() -> None:
    """Disconnect the bot from Azure (it stops reading Data Factory until someone connects it again)."""
    from .connectors.azure_auth import sign_out

    console.print("[green]✓[/] Disconnected." if sign_out() else "The bot wasn't connected.")


@app.command("adf-check")
def adf_check(
    project: Annotated[str | None, typer.Argument(help="Data Factory project (default: all)")] = None,
    days: Annotated[int, typer.Option(help="How far back to look")] = 7,
) -> None:
    """Read-only look at the real factory: pipelines, their schedules and recent runs."""
    from collections import Counter
    from datetime import timedelta

    from .connectors.adf import AdfPipeline
    from .connectors.azure_auth import signed_in_as

    for cfg in _adf_project(project):
        assert cfg.pipeline is not None
        adf = AdfPipeline(cfg.pipeline)
        who = signed_in_as() if cfg.pipeline.auth == "device_code" else "default credential"
        names = adf.watched_pipelines()
        schedules = adf.schedules()
        now = utcnow()
        runs = adf.query_runs(now - timedelta(days=days), now, pipelines=names) if names else []
        t = Table(title=f"{cfg.display_name} · {cfg.pipeline.factory} · signed in as {who}", box=box.SIMPLE_HEAVY)
        for col in ("Pipeline", "Schedule", f"Runs ({days}d)", "Failed", "Last run", "Last status"):
            t.add_column(col)
        for name in names:
            mine = sorted((r for r in runs if r.pipeline == name), key=lambda r: r.started_at or now, reverse=True)
            c = Counter(r.status for r in mine)
            sched = ", ".join(f"{s.trigger} (every {s.every.total_seconds() / 3600:g} h)" for s in schedules.get(name, [])) or "manual / event"
            last = mine[0] if mine else None
            status = last.status if last else "-"
            color = {"Failed": "red", "Succeeded": "green", "InProgress": "cyan"}.get(status, "white")
            t.add_row(name, sched, str(len(mine)), f"[red]{c['Failed']}[/]" if c["Failed"] else "0",
                      f"{last.started_at:%a %d %b %H:%M} UTC" if last and last.started_at else "-", f"[{color}]{status}[/]")
        console.print(t)


@app.command("dq-check")
def dq_check(project: Annotated[str | None, typer.Argument(help="Data Factory project (default: all with data checks)")] = None) -> None:
    """Check the data inside every table the pipelines load, now (opens incidents like a normal cycle).
    With Snowflake browser sign-in, run this once interactively: it opens the sign-in page, then the token is cached."""
    from .engine.cycle import run_cycle

    create_all()
    cfgs = [c for c in _adf_project(project) if c.quality is not None and c.quality.enabled]
    if not cfgs:
        console.print("[yellow]No project has a `quality:` section.[/]")
        raise typer.Exit(1)
    for cfg in cfgs:
        r = run_cycle(cfg, force_quality=True)
        _print_reports([r])
        with session_scope() as s:
            from .api.views import quality_view
            from .db import Project

            p = s.scalars(select(Project).where(Project.key == cfg.project)).one()
            q = quality_view(s, p, cfg, days=1)
        st = q["status"] or {}
        if st.get("unavailable"):
            console.print(f"[red]Couldn't reach the warehouse:[/] {st.get('message')}")
            continue
        t = Table(title=f"{cfg.display_name} · data quality · score {q['summary']['score']}", box=box.SIMPLE_HEAVY)
        for col in ("Table", "Rows today", "Fresh (h)", "Dupes", "Worst empty column", "Failing"):
            t.add_column(col)
        for x in q["tables"]:
            worst = x["nulls"][0] if x["nulls"] else None
            failing = [c["label"] for c in x["checks"] if c["status"] == "fail"]
            t.add_row(x["table"], f"{x['rows_today']:,.0f}" if x["rows_today"] is not None else "-",
                      f"{x['freshness_hours']:.1f}" if x["freshness_hours"] is not None else "-",
                      f"{x['duplicate_rate']:.2%}" if x["duplicate_rate"] is not None else "-",
                      f"{worst['column']} {worst['rate']:.1%}" if worst else "-",
                      f"[red]{', '.join(failing)}[/]" if failing else "[green]none[/]")
        console.print(t)


@app.command("morning-report")
def morning_report(
    project: Annotated[str | None, typer.Argument(help="Project (default: all)")] = None,
    send: Annotated[bool, typer.Option(help="Also send it (Teams via the project's notifier), even if today's was sent.")] = False,
    ai: Annotated[bool, typer.Option(help="Add the AI summary paragraph.")] = True,
) -> None:
    """Print the Daily Morning Report (the worker sends it automatically at alerts.morning_report)."""
    from .engine.report import ai_summary, build_report, report_text, send_report

    create_all()
    for cfg in [c for c in _selected(project) if c.active or project]:
        if send:
            r = send_report(cfg, force=True, use_ai=ai)
        else:
            from .engine.cycle import make_context

            with session_scope() as s:
                r = build_report(s, make_context(s, cfg).project, cfg)
            if ai and (summary := ai_summary(r)):
                r["ai_summary"], r["ai_model"] = summary
                r["text"] = report_text(r)
        if r:
            console.print(r["text"], highlight=False)
            console.print()


# ── members ───────────────────────────────────────────────────────────────────


@members_app.command("list")
def members_list() -> None:
    with session_scope() as s:
        t = Table(box=box.SIMPLE)
        for col in ("Email", "Name", "Admin", "Projects"):
            t.add_column(col)
        for u in s.scalars(select(User).order_by(User.email)):
            roles = s.execute(select(Project.key, UserProject.role).join(UserProject, UserProject.project_id == Project.id)
                              .where(UserProject.user_id == u.id)).all()
            t.add_row(u.email, u.name, "yes" if u.is_admin else "", ", ".join(f"{k} ({r})" for k, r in roles) or "[dim]none[/]")
    console.print(t)


@members_app.command("add")
def members_add(
    email: str,
    project: str,
    role: Annotated[str, typer.Option(help="viewer or owner")] = "viewer",
    name: Annotated[str | None, typer.Option(help="Display name for a new user.")] = None,
) -> None:
    from .engine.cycle import ensure_project
    from .seed import set_membership

    if role not in ("viewer", "owner"):
        raise typer.BadParameter("role must be viewer or owner")
    cfg = _project(project)
    with session_scope() as s:
        user = s.scalars(select(User).where(User.email == email.lower())).first()
        if user is None:
            user = User(email=email.lower(), name=name or email.split("@")[0])
            s.add(user)
            s.flush()
        set_membership(s, user, ensure_project(s, cfg), role)
    console.print(f"[green]✓[/] {email} is now {role} of {cfg.display_name}")


@members_app.command("remove")
def members_remove(email: str, project: str) -> None:
    with session_scope() as s:
        user = s.scalars(select(User).where(User.email == email.lower())).first()
        p = s.scalars(select(Project).where(Project.key == project)).first()
        link = s.get(UserProject, (user.id, p.id)) if user and p else None
        if link is None:
            console.print("[yellow]no such membership[/]")
            return
        s.delete(link)
    console.print(f"[green]✓[/] removed {email} from {project}")


@app.command(hidden=True)
def wipe() -> None:
    """Delete local prototype state (.data/)."""
    shutil.rmtree(".data", ignore_errors=True)
    console.print("[green]✓[/] .data/ removed")


__all__ = ["app", "CheckResult"]
