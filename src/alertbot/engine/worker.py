"""The worker's round: watch Azure for new runs, run what's due, scan costs and values, send the morning reports.

`alertbot worker` runs it in a loop in its own process. On a host where the Microsoft sign-in cache lives only in
memory (no OS encryption, e.g. Linux on Render), the server runs the same loop in a thread instead
(ALERTBOT_EMBEDDED_WORKER=true), so it can use the sign-in people made on the portal.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)


def run_round(say: Callable[[str], None], estates: list | None = None,
              show_reports: Callable[[list], None] | None = None) -> None:
    """One round. `say` gets a short rich-markup line for each thing worth telling; `show_reports` the scheduled rounds."""
    from ..discovery import rediscover_all
    from ..dq.storage import scan_all
    from ..dq.suspicious import scan_due
    from .cycle import run_due
    from .report import due_reports
    from .watch import watch_all

    for est in estates or []:
        n = est.catch_up()
        if n:
            say(f"[dim]{est.cfg.project}: {n} scheduled scrape(s) simulated[/]")
    for key, changes, report in watch_all():
        what = ", ".join(f"{c.pipeline} {c.status.lower()}" for c in changes[:3]) + (f" +{len(changes) - 3}" if len(changes) > 3 else "")
        say(f"[cyan]{key}[/]: {what} → checked ({report.status}, {report.changes.opened} opened, {report.changes.resolved} resolved)")
    reports = run_due()
    if reports:
        if show_reports:
            show_reports(reports)
        else:
            say(f"{len(reports)} scheduled round(s) run")
    rediscover_all()  # every few hours: what each person can see in Azure (new resources, revoked access)
    scan_all()  # hourly: each connected database's table sizes and usage (kept, so a restart doesn't erase it)
    try:  # its own guard, like the next one
        from ..dq.compute import scan_all as compute_scan_all

        compute_scan_all()  # Snowflake compute: hourly, what's running every 10 minutes; new alerts to Teams
    except Exception:  # noqa: BLE001
        log.exception("compute cost scan failed this round")
    try:  # Snowflake Cortex AI usage: hourly, its own guard
        from ..dq.cortex import scan_all as cortex_scan_all

        cortex_scan_all()
    except Exception:  # noqa: BLE001
        log.exception("Cortex AI usage scan failed this round")
    try:  # its own guard: a problem here must never hold up the morning report below
        scan_due()  # each checked table: rules from its column names (once), and rows breaking them, on its own schedule
    except Exception:  # noqa: BLE001
        log.exception("suspicious-value checks failed this round")
    for key in due_reports():
        say(f"[dim]{key}: morning report sent[/]")


def start_in_thread(tick: int) -> threading.Event:
    """Run rounds every `tick` seconds in a daemon thread; set the returned event to stop it."""
    from rich.text import Text

    stop = threading.Event()

    def say(m: str) -> None:  # straight to the server's output: uvicorn shows only warnings from other loggers
        print(f"WORKER:   {Text.from_markup(m).plain}", flush=True)

    def loop() -> None:
        say(f"started inside the server · watching Azure for new runs every {tick}s")
        while not stop.is_set():
            try:
                run_round(say)
            except Exception:  # noqa: BLE001 - one bad round (e.g. a locked database) must not stop the worker
                log.exception("worker round failed; trying again in %ss", tick)
            stop.wait(tick)

    threading.Thread(target=loop, name="alertbot-worker", daemon=True).start()
    return stop
