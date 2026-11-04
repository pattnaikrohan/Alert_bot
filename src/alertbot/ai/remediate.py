"""What can be safely done about an incident, with one recommended option.

The bot stays read-only (as the requirement asks, until it has earned trust): options that change something in the
factory open Data Factory Studio at the right place for a person to do it; the only actions the bot takes itself are
its own (mute the alerts, accept a new schema as the baseline, re-check the data).
"""

from __future__ import annotations

import re
from typing import Any

TRANSIENT = re.compile(r"timeout|timed out|throttl|429|temporar|transient|connection (?:was )?(?:reset|closed|refused)|"
                       r"serviceunavailable|service unavailable|internal server error|deadlock|try again", re.I)
NO_DATA = re.compile(r"empty array|nothing to load|no matching file|filenotfound|does not exist|path .* not found|no files? found", re.I)
FORMAT = re.compile(r"morecolumnsthandefined|fewercolumns|column|schema|conversion|cannot convert|datatype|data type|delimited|parse", re.I)
ACCESS = re.compile(r"unauthori[sz]ed|forbidden|403|401|credential|password|token|permission|access denied|login failed", re.I)


def _opt(key: str, label: str, what: str, *, href: str | None = None, action: str | None = None, changes_factory: bool = False) -> dict[str, Any]:
    return {"key": key, "label": label, "what": what, "href": href, "action": action, "changes_factory": changes_factory}


def remediation(incident: Any, checks: dict[str, Any], *, monitor_url: str | None, studio_url: str | None,
                teams_channel: str = "") -> dict[str, Any] | None:
    """Options for an incident (`checks`: check name → latest CheckResult), the recommended one first, and why."""
    names = set(checks)
    scope = incident.scope
    notify = _opt("notify", "Notify data engineering", f"The alert went to {teams_channel or 'the team channel'}; "
                  "acknowledge it here so everyone sees who's on it.", action="acknowledge")
    ignore = _opt("ignore", "Ignore for 24 h", "Mute this incident's alerts for a day (it stays visible here).", action="mute")
    retry = _opt("retry", "Retry the run", "Rerun the failed pipeline run from Data Factory's monitor (Rerun).",
                 href=monitor_url, changes_factory=True)
    from_failed = _opt("rerun_failed", "Rerun from the failed step", "In the monitor: Rerun from failed activity; the steps that "
                       "already succeeded are not repeated.", href=monitor_url, changes_factory=True)
    new_normal = _opt("accept_normal", "This is expected now", "The change is expected (e.g. more data from now on): the bot "
                      "learns the new level from these runs and stops alerting on it.", action="accept_normal")
    backfill = _opt("backfill", "Backfill the missing load", "Trigger the pipeline for the missed window (Add trigger → Trigger now, "
                    "with the date parameters of the missed run).", href=studio_url, changes_factory=True)

    if scope.startswith("data:"):
        accept = _opt("accept_schema", "Accept the new schema", "Make today's columns the new baseline (when the change is intended).",
                      action="accept_schema")
        recheck = _opt("recheck", "Check the data again", "Re-run every data check on this table now.", action="recheck")
        if "freshness" in names:
            return _pack([backfill, recheck, notify, ignore], "backfill",
                         "New rows haven't arrived: load the missing window once the source has delivered.")
        if "schema_drift" in names and len(names) == 1:
            return _pack([accept, notify, ignore], "accept_schema", "Only the columns changed: if that was planned, accept it; "
                         "otherwise tell whoever owns the source.")
        if "duplicates" in names:
            return _pack([notify, recheck, new_normal, ignore], "notify", "Duplicates need a de-duplication step or a fix at the source; "
                         "re-running the load would add more.")
        return _pack([notify, recheck, new_normal, backfill, ignore], "notify", "The data itself looks wrong: confirm with the data owner before "
                     "reloading; a reload copies the same source again.")

    if "run_failed" in names:
        err = str(checks["run_failed"].details.get("error") or checks["run_failed"].message or "")
        if NO_DATA.search(err):
            return _pack([notify, retry, ignore], "notify", "There was nothing to load (no matching file). Retrying now fails the "
                         "same way; wait for the source files, then retry.")
        if ACCESS.search(err):
            return _pack([notify, retry, ignore], "notify", "A sign-in or permission problem: the platform team has to fix the "
                         "connection before a retry can work.")
        if FORMAT.search(err):
            return _pack([notify, from_failed, ignore], "notify", "The input doesn't match the expected format; fix the file or the "
                         "mapping first, then rerun from the failed step.")
        if TRANSIENT.search(err):
            return _pack([retry, from_failed, notify, ignore], "retry", "This looks temporary (a timeout or busy service): retry once.")
        return _pack([from_failed, retry, notify, ignore], "rerun_failed", "Rerun from the failed step once the cause above is "
                     "understood; the steps that worked aren't repeated.")
    if "missed_run" in names:
        return _pack([backfill, notify, ignore], "backfill", "The scheduled run didn't happen: check the trigger is started, then "
                     "run the missed window.")
    if names & {"sla_risk", "run_too_slow"}:
        return _pack([notify, new_normal, ignore] if "run_too_slow" in names else [notify, ignore], "notify", "It's still running: let it finish, and warn the people who use its tables that "
                     "today's data will be late.")
    if names & {"row_count", "empty_site", "rows_skipped"}:
        return _pack([notify, new_normal, backfill, ignore], "notify", "The load ran but delivered an unusual amount: check the source first; "
                     "reload only once the source is complete.")
    return _pack([notify, ignore], "notify", "Look at the analysis above first.")


def _pack(options: list[dict[str, Any]], recommended: str, why: str) -> dict[str, Any]:
    options = [o for o in options if o["href"] or o["action"] or not o["changes_factory"]]
    return {"mode": "read_only", "recommended": recommended if any(o["key"] == recommended for o in options) else options[0]["key"],
            "why": why, "options": options}
