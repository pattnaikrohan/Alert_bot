"""The bot's assistant: questions answered from the bot's own facts, and the whole app driven in plain English.

    "ack the pl_sql one"                 → acknowledge it
    "resolve #10, re-ran the pipeline"   → resolve, with that note
    "mute pl_uat for 2 hours"            → mute its Teams alerts
    "send the alert for #12 to Teams"    → send it now
    "ack everything critical"            → acknowledge each critical incident nobody's on
    "send the morning report" · "run the checks" · "refresh everything" · "take me to lineage" · "why did pl_sql fail?"

Nothing changes without the person's click: every change comes back as a proposal (what will happen, to what), and the
API re-checks it from scratch, with that person's own rights, when they confirm. Plain commands are understood without
any AI (so they work when the models are down); the AI plans anything more open-ended, and its plan is held to the same
proposal builders, so it can never ask for something the rules wouldn't.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import load_ai_config, load_projects
from ..db import Incident, Mute, Project, User, UserProject
from ..engine.checks import SEVERITY_RANK
from ..timeutil import iso, utcnow

ACTIVE = ("open", "acknowledged")

# ── what the bot can do ───────────────────────────────────────────────────────

TOOLS: dict[str, dict[str, Any]] = {
    "acknowledge": {"label": "Acknowledge", "owner": False, "args": "incidents: [ids]",
                    "does": "Say you're on it: the incident stops counting as 'needs attention' (it stays open until resolved)."},
    "resolve": {"label": "Resolve", "owner": False, "args": "incidents: [ids], note: text (how it was fixed)",
                "does": "Close it now, with your name and note. If its checks still fail, the bot opens a new incident."},
    "comment": {"label": "Comment", "owner": False, "args": "incident: id, text",
                "does": "Add a note to the incident's record (everyone on the project sees it)."},
    "accept_normal": {"label": "Mark as expected", "owner": False, "args": "incident: id",
                      "does": "The bot learns this level is normal from now on (only for row counts, durations, empty shares…)."},
    "send_alert": {"label": "Send to Teams", "owner": False, "args": "incident: id",
                   "does": "Send the incident's alert card to the project's Teams channel now."},
    "mute": {"label": "Mute alerts", "owner": True, "args": "project, target: incident id | pipeline | 'project', hours",
             "does": "No Teams alerts for that scope for a while (the bot keeps watching and the app still shows it)."},
    "unmute": {"label": "Unmute alerts", "owner": True, "args": "project, mute: id | 'all'", "does": "Alerts flow again."},
    "send_report": {"label": "Send the morning report", "owner": True, "args": "project", "does": "Post today's report to Teams now."},
    "run_checks": {"label": "Run every check now", "owner": False, "args": "project (optional)",
                   "does": "Read the latest runs and run every check (read-only)."},
    "refresh": {"label": "Refresh everything", "owner": False, "args": "",
                "does": "Re-read Azure, every pipeline, your databases, then run every check (read-only)."},
}

PAGES = {"home": "/", "home page": "/", "start": "/", "dashboard": "/",
         "incidents": "/incidents", "all incidents": "/incidents", "incident register": "/incidents", "register": "/incidents",
         "problems": "/incidents", "runs": "/runs", "all runs": "/runs", "run history": "/runs",
         "pipelines": "/pipelines", "all pipelines": "/pipelines", "pipeline list": "/pipelines", "logic apps": "/pipelines",
         "pipeline report": "/pipeline-report", "pipeline health": "/pipeline-report", "health report": "/pipeline-report",
         "lineage": "/lineage", "data lineage": "/lineage", "impact": "/lineage",
         "databases": "/databases", "database": "/databases", "connections": "/databases", "snowflake": "/databases",
         "cost": "/compute", "costs": "/compute", "compute": "/compute", "compute cost": "/compute", "snowflake cost": "/compute",
         "snowflake compute": "/compute", "credits": "/compute", "spend": "/compute", "budget": "/compute",
         "warehouse": "/warehouse", "warehouse cost": "/warehouse", "storage": "/warehouse", "storage cost": "/warehouse",
         "unused tables": "/warehouse", "cleanup": "/warehouse", "clean up": "/warehouse",
         "ai usage": "/ai-usage", "native ai usage": "/ai-usage", "native ai": "/ai-usage", "ai cost": "/ai-usage", "ai costs": "/ai-usage", "tokens": "/ai-usage",
         "cortex": "/cortex", "cortex ai": "/cortex", "cortex ai usage": "/cortex", "cortex usage": "/cortex", "cortex cost": "/cortex",
         "snowflake ai": "/cortex", "snowflake cortex": "/cortex",
         "the bot": "/bot", "bot": "/bot", "robot": "/bot"}
# a request names a page with its longest matching words first ("pipeline report" before "pipelines")
PAGES = dict(sorted(PAGES.items(), key=lambda kv: -len(kv[0])))
PROJECT_PAGES = {"overview": "", "incidents": "/incidents", "incident register": "/incidents", "register": "/incidents", "runs": "/runs",
                 "tables": "/tables", "data quality": "/tables?tab=quality", "morning report": "/report", "report": "/report",
                 "alerts": "/alerts", "mutes": "/alerts"}


# Things the bot changes in the app itself, for this person only (no confirm: they change the view, not any data).
UI_DOES = {"style": "modern | classic (calm, formal, for management)", "theme": "light | dark", "layout": "dock | side", "tour": "start", "fullscreen": "on | off", "search": "open"}
UI_RULES: list[tuple[str, str, Any]] = [
    (r"\b(classic|management|executive|formal|professional)\s+(style|look|view|mode|layout|design|ui)\b|"
     r"^(?:please\s+)?(?:can you\s+|could you\s+)?(?:switch|change|go|move|set|make it|turn it|put it|use)(?:\s+(?:back|it|the app|the look|the style|over))*\s+(?:to|into|in)?\s*(?:the\s+)?classic(?:\s+one)?[\s.!]*$|^classic[\s.!]*$", "style", "classic"),
    (r"\b(modern|colou?rful|fancy|default)\s+(style|look|view|mode|layout|design|ui)\b|"
     r"^(?:please\s+)?(?:can you\s+|could you\s+)?(?:switch|change|go|move|set|make it|turn it|put it|use)(?:\s+(?:back|it|the app|the look|the style|over))*\s+(?:to|into|in)?\s*(?:the\s+)?modern(?:\s+one)?[\s.!]*$|^modern[\s.!]*$", "style", "modern"),
    (r"\b(dark|night)\s*(mode|theme)\b|\b(go|switch|change|turn|make it|set it to|use)\s+(to\s+)?(the\s+)?dark\b", "theme", "dark"),
    (r"\b(light|day)\s*(mode|theme)\b|\b(go|switch|change|turn|make it|set it to|use)\s+(to\s+)?(the\s+)?light\b", "theme", "light"),
    (r"\b(side\s*(panel|bar|nav(igation)?|menu)|sidebar)\b|\b(to|on|at|in)\s+the\s+(left(\s+hand)?\s+)?side\b|\b(to|on)\s+the\s+left\b", "layout", "side"),
    (r"\b(bottom\s+)?dock\b|\bbottom\s+(bar|nav(igation)?)\b", "layout", "dock"),
    (r"\b(exit|leave|close|stop|turn off)\s+full\s*-?\s*screen\b", "fullscreen", "off"),
    (r"\bfull\s*-?\s*screen\b", "fullscreen", "on"),
    (r"\b(tour|walk ?through|walk me through|show me around|guide me|onboarding)\b", "tour", "start"),
    (r"^(?:please\s+)?(?:open\s+(?:the\s+)?search|search(?:\s+(?:the\s+app|everything))?|find something)[.!?]*$", "search", "open"),
]
UI_SAY = {("style", "classic"): "Switched to the Classic style: calm and formal, with the menu on the side.",
          ("style", "modern"): "Switched to the Modern style: the dock, colour and motion are back.",
          ("theme", "dark"): "Switched to the dark theme.", ("theme", "light"): "Switched to the light theme.",
          ("layout", "side"): "Moved the navigation to a side panel.", ("layout", "dock"): "Moved the navigation back to the bottom dock.",
          ("fullscreen", "on"): "Going full screen. Press Esc to leave.", ("fullscreen", "off"): "Leaving full screen.",
          ("tour", "start"): "Starting the tour: two minutes and you'll know your way around.",
          ("search", "open"): "Search is open: type a pipeline, Logic App, table or page."}


def ui_command(t: str) -> dict[str, str] | None:
    """A request to change the app itself ("dark mode", "move the menu to the side", "start the tour")."""
    if not re.search(r"\b(mode|theme|dark|light|panel|sidebar|side|dock|bar|nav|navigation|menu|screen|tour|walk|around|guide|onboarding|search|find|"
                     r"classic|modern|style|look|view|layout)\b", t):
        return None
    for pat, do, value in UI_RULES:
        if re.search(pat, t):
            return {"do": do, "value": value}
    return None


class Refusal(Exception):
    """A request the bot can't turn into an action, with what to say instead."""


@dataclass
class World:
    user: User
    projects: dict[str, dict[str, Any]]
    incidents: list[dict[str, Any]]
    mutes: list[dict[str, Any]]
    current: str | None = None  # the project page the person is on

    def project(self, key: str | None) -> dict[str, Any] | None:
        return self.projects.get(key or "") if key else None

    def incident(self, iid: int) -> dict[str, Any] | None:
        return next((i for i in self.incidents if i["id"] == iid), None)

    def pipelines(self) -> list[tuple[str, str]]:
        return [(p["key"], n) for p in self.projects.values() for n in p["pipelines"]]


def world_of(session: Session, user: User, current: str | None = None) -> World:
    from ..engine.adf import inventory_of

    cfgs = load_projects()
    links = session.execute(select(Project, UserProject.role).join(UserProject, UserProject.project_id == Project.id)
                            .where(UserProject.user_id == user.id, Project.active.is_(True))).all()
    projects: dict[str, dict[str, Any]] = {}
    for p, role in links:
        if p.key not in cfgs:
            continue
        cfg = cfgs[p.key]
        projects[p.key] = {"key": p.key, "id": p.id, "name": p.display_name, "role": "owner" if user.is_admin else role,
                           "kind": cfg.kind, "pipelines": [i["name"] for i in inventory_of(session, p.key).get("items", [])][:200],
                           "notifier": cfg.alerts.notifier}
    by_id = {v["id"]: v for v in projects.values()}
    incidents = []
    for i in session.scalars(select(Incident).where(Incident.project_id.in_(list(by_id) or [-1]), Incident.status.in_(ACTIVE))):
        p = by_id[i.project_id]
        kind, _, name = i.scope.partition(":")
        incidents.append({"id": i.id, "project": p["key"], "project_name": p["name"], "title": i.title, "severity": i.severity,
                          "status": i.status, "scope": i.scope, "site": name if kind == "site" else None,
                          "table": name if kind in ("table", "data") else None, "acknowledged_by": i.acknowledged_by,
                          "opened_at": iso(i.opened_at), "checks": i.checks, "likely_cause": (i.likely_cause or "").split("\n")[0][:200]})
    incidents.sort(key=lambda i: (i["status"] != "open", -SEVERITY_RANK.get(i["severity"], 0), i["opened_at"] or ""), reverse=False)
    mutes = [{"id": m.id, "project": next(k for k, v in projects.items() if v["id"] == m.project_id), "scope": m.scope,
              "until": iso(m.until), "reason": m.reason, "by": m.created_by}
             for m in session.scalars(select(Mute).where(Mute.project_id.in_(list(by_id) or [-1]), Mute.until > utcnow()))]
    return World(user=user, projects=projects, incidents=incidents, mutes=mutes, current=current if current in projects else None)


# ── proposals (the only way anything gets done) ───────────────────────────────


def _label(i: dict[str, Any]) -> str:
    return f"#{i['id']} {i['title']}"


def propose(tool: str, args: dict[str, Any], w: World) -> dict[str, Any]:
    """A change, checked against what exists and what this person may do, written out so they can confirm it."""
    if tool not in TOOLS:
        raise Refusal("I can't do that one.")
    spec = TOOLS[tool]
    detail: list[str] = []
    project: str | None = args.get("project")
    clean: dict[str, Any] = {}

    def incidents(ids: list[Any], allowed: tuple[str, ...]) -> list[dict[str, Any]]:
        out = []
        for raw in ids:
            try:
                inc = w.incident(int(raw))
            except (TypeError, ValueError):
                continue
            if inc is None:
                raise Refusal(f"I can't find an open incident #{raw} in your projects (it may be resolved already).")
            if inc["status"] not in allowed:
                raise Refusal(f"#{inc['id']} is already {inc['status']}.")
            out.append(inc)
        if not out:
            raise Refusal("Which incident? Tell me its number, e.g. #12, or the pipeline it's about.")
        return out

    if tool in ("acknowledge", "resolve"):
        incs = incidents(args.get("incidents") or ([args["incident"]] if args.get("incident") else []),
                         ("open",) if tool == "acknowledge" else ACTIVE)
        clean = {"incidents": [i["id"] for i in incs]}
        if tool == "resolve" and (note := str(args.get("note") or "").strip()):
            clean["note"] = note[:1000]
        detail = [_label(i) for i in incs] + ([f"Note: “{clean['note']}”"] if clean.get("note") else [])
        title = f"{spec['label']} {len(incs)} incident{'s' if len(incs) > 1 else ''}" if len(incs) > 1 else f"{spec['label']} #{incs[0]['id']}"
        project = incs[0]["project"]
        projects = {i["project"] for i in incs}
    elif tool in ("comment", "accept_normal", "send_alert"):
        inc = incidents([args.get("incident")] if args.get("incident") else (args.get("incidents") or [])[:1], ACTIVE)[0]
        clean = {"incident": inc["id"]}
        project, projects = inc["project"], {inc["project"]}
        if tool == "comment":
            text = str(args.get("text") or "").strip()
            if not text:
                raise Refusal(f"What should the comment on #{inc['id']} say?")
            clean["text"] = text[:2000]
            detail = [_label(inc), f"“{clean['text']}”"]
            title = f"Comment on #{inc['id']}"
        elif tool == "accept_normal":
            if not any(c in ("row_count", "empty_site", "rows_skipped", "run_too_slow", "null_spike", "duplicates", "freshness",
                             "distribution_shift", "value_range", "out_of_range", "invalid_type", "business_rule",
                             "referential_integrity", "missing_partitions") for c in inc["checks"]):
                raise Refusal(f"#{inc['id']} is about {', '.join(inc['checks'])}: a failed run or a missed schedule can't become 'normal'.")
            detail, title = [_label(inc), "The bot learns from the newer runs from now on"], f"Mark #{inc['id']} as expected"
        else:
            if w.projects[inc["project"]]["notifier"] == "none":
                raise Refusal(f"{inc['project_name']} has no Teams channel set up, so there's nowhere to send it.")
            muted = next((m for m in w.mutes if m["project"] == inc["project"] and m["scope"] in ("*", inc["scope"])), None)
            if muted:
                raise Refusal(f"Alerts for #{inc['id']} are muted until {muted['until']} (by {muted['by']}). Say “unmute” first if you want it sent.")
            detail, title = [_label(inc), f"To {inc['project_name']}'s Teams channel"], f"Send #{inc['id']} to Teams"
    elif tool == "mute":
        target = args.get("target")
        hours = float(args.get("hours") or 4)
        if not 0 < hours <= 24 * 14:
            raise Refusal("I can mute for up to 14 days.")
        scope, what = "*", None
        if isinstance(target, int) or (isinstance(target, str) and target.lstrip("#").isdigit()):
            inc = incidents([int(str(target).lstrip("#"))], ACTIVE)[0]
            scope, project, what = inc["scope"], inc["project"], _label(inc)
        elif target and target != "project":
            hit = next(((k, n) for k, n in w.pipelines() if n.lower() == str(target).lower() and (not project or k == project)), None)
            if hit is None:
                raise Refusal(f"I don't know a pipeline called {target}.")
            project, scope, what = hit[0], f"site:{hit[1]}", f"Pipeline {hit[1]}"
        project = project or w.current or (next(iter(w.projects)) if len(w.projects) == 1 else None)
        if project not in w.projects:
            raise Refusal("Which project should I mute?")
        projects = {project}
        clean = {"project": project, "target": target if target not in (None, "") else "project", "scope": scope, "hours": hours,
                 "reason": str(args.get("reason") or "Muted from the assistant")[:200]}
        span = span_label(hours)
        detail = [what or f"Every alert in {w.projects[project]['name']}", f"{span[0].upper() + span[1:] if span.startswith('until') else 'For ' + span}; the bot keeps watching"]
        title = f"Mute alerts {span}" if span.startswith("until") else f"Mute alerts for {span}"
    elif tool == "unmute":
        project = project or w.current or (next(iter(w.projects)) if len(w.projects) == 1 else None)
        mine = [m for m in w.mutes if not project or m["project"] == project]
        which = args.get("mute")
        if args.get("mutes"):
            mine = [m for m in mine if m["id"] in {int(x) for x in args["mutes"] if str(x).isdigit()}]
        elif which not in (None, "all"):
            mine = [m for m in mine if str(m["id"]) == str(which)]
        if not mine:
            raise Refusal("Nothing is muted right now.")
        projects = {m["project"] for m in mine}
        project = mine[0]["project"]
        clean = {"mutes": [m["id"] for m in mine], "project": project}
        detail = [f"{'Everything' if m['scope'] == '*' else m['scope'].split(':', 1)[-1]} in {w.projects[m['project']]['name']} (until {m['until']})" for m in mine]
        title = f"Unmute {len(mine)} alert scope{'s' if len(mine) > 1 else ''}"
    elif tool in ("send_report", "run_checks"):
        project = project or w.current or (next(iter(w.projects)) if len(w.projects) == 1 else None)
        if tool == "send_report" and project not in w.projects:
            raise Refusal("Which project's report should I send?")
        if project and project not in w.projects:
            raise Refusal("That's not one of your projects.")
        projects = {project} if project else set(w.projects)
        clean = {"project": project} if project else {}
        title = f"{spec['label']}{' for ' + w.projects[project]['name'] if project else ' for every project'}"
        detail = [spec["does"]]
    else:  # refresh
        projects, clean, title, detail = set(w.projects), {}, spec["label"], [spec["does"]]

    blocked = [k for k in projects if spec["owner"] and w.projects[k]["role"] != "owner"]
    return {"id": uuid.uuid4().hex[:12], "tool": tool, "args": clean, "title": title, "detail": detail, "does": spec["does"],
            "owner_only": spec["owner"], "allowed": not blocked,
            "why_not": f"Only a project owner can {spec['label'].lower()} in {', '.join(w.projects[k]['name'] for k in blocked)}." if blocked else None,
            "project": project}


# ── understanding plain commands (no AI needed) ───────────────────────────────

VERBS = [
    ("unmute", r"\b(un-?mute|unsilence|resume alerts|turn alerts back on)\b"),
    ("mute", r"\b(mute|silence|snooze|quiet|stop (the )?alerts?)\b"),
    ("accept_normal", r"\b(new normal|accept (it|this|that|#?\d+)? ?as normal|that'?s (the )?normal|is expected now)\b"),
    ("send_report", r"\b(send|post|share|push)\b.*\b(morning|daily) report\b|\b(morning|daily) report\b.*\b(to teams|now)\b"),
    ("send_alert", r"\b(send|resend|re-send|push|notify|ping|post|escalate)\b.*\b(alert|teams|notification|channel|team)\b|\balert (the )?team\b"),
    ("comment", r"\b(comment|add a note|note (on|to|for)|write on|leave a note)\b"),
    ("resolve", r"\b(resolve|close|mark (it |this |that |them )?(as )?(fixed|resolved|done)|it'?s fixed|is fixed)\b"),
    ("acknowledge", r"\b(ack|acknowledge|acknowledged|i'?m on it|we'?re on it|take (it|this|that)|claim|own (it|this|that))\b"),
    ("refresh", r"\b(refresh|sync|re-?read)\b(?!.*\bpage\b)"),
    ("run_checks", r"\b(run|re-?run|do)\b.*\bchecks?\b|\bcheck (everything|now|all|again)\b"),
]
NAV = re.compile(r"^(?:please\s+)?(?:open|go to|goto|take me to|navigate to|bring up|show me|show)\s+(?:the\s+|my\s+)?(.+?)(?:\s+page)?[.!?]*$")
HELLO = re.compile(r"^(hi|hello|hey|yo|good (morning|afternoon|evening)|hiya|howdy)\b[!. ]*$")
THANKS = re.compile(r"^(thanks|thank you|thx|cheers|great|perfect|nice|awesome|ok(ay)?|cool)\b[!. ]*$")
CAPS = re.compile(r"\b(what can you do|help|commands|how do i use you|capabilities)\b")


def span_label(hours: float) -> str:
    """30 minutes · 2 hours · 3 days · or, for an uneven span (till tomorrow morning), the time it ends."""
    from datetime import datetime, timedelta

    from ..settings import get_settings

    if hours < 1:
        return f"{round(hours * 60)} minutes"
    if abs(hours - round(hours)) < 0.05:
        h = round(hours)
        return f"{h} hour{'s' if h != 1 else ''}" if h < 48 or h % 24 else f"{h // 24} days"
    end = datetime.now(get_settings().tz) + timedelta(hours=hours)
    return f"until {end:%a %H:%M}"


def _duration(t: str) -> float | None:
    """Hours, from "for 2 hours", "30 min", "half an hour", "a day", "till tomorrow (morning)", "for the rest of the day"."""
    from datetime import datetime, timedelta

    from ..settings import get_settings

    m = re.search(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?|w|weeks?)\b", t)
    if m:
        n, u = float(m.group(1)), m.group(2)[0]
        return n / 60 if u == "m" else n * 24 if u == "d" else n * 168 if u == "w" else n
    for pat, hours in ((r"half an? hour", 0.5), (r"\ban hour\b|\bone hour\b", 1.0), (r"\ba day\b|\bone day\b", 24.0), (r"\ba week\b", 168.0)):
        if re.search(pat, t):
            return hours
    now = datetime.now(get_settings().tz)
    if re.search(r"\b(till|until|through|to)\s+tomorrow\b|\bovernight\b|\btill (the )?morning\b", t):
        until = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        return round((until - now).total_seconds() / 3600, 2)
    if re.search(r"\b(today|for the day|rest of the day|end of (the )?day|eod)\b", t):
        until = now.replace(hour=23, minute=59, second=0, microsecond=0)
        return max(0.25, round((until - now).total_seconds() / 3600, 2))
    return None


def _targets(t: str, w: World, focus: int | None, tool: str) -> list[int]:
    ids = [int(x) for x in re.findall(r"(?:#\s?|\bincidents?\s+|\bnumber\s+)(\d{1,7})\b", t)]
    if not ids:
        ids = [int(x) for x in re.findall(r"\b(\d{1,7})\b(?!\s*(?:m|min|minutes?|h|hrs?|hours?|d|days?|w|weeks?)\b)", t)
               if w.incident(int(x))]
    if ids:
        return ids
    pool = [i for i in w.incidents if (i["status"] == "open" if tool == "acknowledge" else True)]
    if w.current and re.search(r"\b(here|this project)\b", t):
        pool = [i for i in pool if i["project"] == w.current]
    for key, p in w.projects.items():
        if re.search(rf"\b{re.escape(p['name'].lower().split(' (')[0])}\b", t) or re.search(rf"\b{re.escape(key)}\b", t):
            pool = [i for i in pool if i["project"] == key]
    named = [i for i in pool if any(n and re.search(rf"(?<![\w-]){re.escape(n.lower())}(?![\w-])", t)
                                    for n in (i["site"], (i["table"] or "").split(".")[-1] or None))]
    if named:
        pool = named
    if re.search(r"\b(all|every(thing| one)?|them all|all of them)\b", t):
        if "critical" in t:
            pool = [i for i in pool if i["severity"] == "critical"]
        elif "warning" in t:
            pool = [i for i in pool if i["severity"] == "warning"]
        return [i["id"] for i in pool]
    if named:
        return [named[0]["id"]]
    if re.search(r"\b(worst|top|most (urgent|critical)|first one|the critical one)\b", t) and pool:
        return [pool[0]["id"]]
    if focus and re.search(r"\b(it|this|that|this one|that one|the same|them)\b", t):
        return [focus]
    if len(pool) == 1 and tool in ("acknowledge", "resolve", "send_alert", "comment", "accept_normal"):
        return [pool[0]["id"]]
    return []


def _quoted_or_after(original: str, keys: str) -> str:
    q = re.search(r"\"(.+?)\"|“(.+?)”|(?:^|\s)'(.+?)'(?=\s|[.!?]|$)|‘(.+?)’", original)  # an apostrophe inside isn't a quote
    if q:
        return next(g for g in q.groups() if g).strip()
    m = re.search(rf"(?:{keys})\s*[:\-–—]?\s*(.+)$", original, re.I)
    return m.group(1).strip(" .") if m else ""


def parse(message: str, w: World, focus: int | None) -> dict[str, Any] | None:
    """A plain command → {"tool", "args"} | {"navigate": path, "reply"} | {"say": ...}; None when it isn't a command."""
    original = message.strip()
    t = " ".join(original.lower().split())
    if HELLO.match(t):
        return {"say": "hello"}
    if THANKS.match(t):
        return {"say": "thanks"}
    if CAPS.search(t) and len(t) < 60:
        return {"say": "help"}
    if ui := ui_command(t):
        return {"ui": ui, "reply": UI_SAY[(ui["do"], ui["value"])]}
    if m := re.match(r"^(?:please\s+)?(?:switch|change|swap|move|go|jump|open|show)\s+(?:me\s+)?(?:to\s+|over to\s+)?(?:the\s+)?(?:project\s+)?(.+?)(?:\s+project)?[.!?]*$", t):
        name = m.group(1).strip()
        hit = next((k for k, p in w.projects.items() if name in (k, p["name"].lower(), p["name"].lower().split(" (")[0])), None)
        if hit:
            return {"navigate": f"/p/{hit}", "reply": f"Switched to {w.projects[hit]['name']}."}
    if re.search(r"\bhow (?:do|can|should|would) (?:i|we|you) (?:fix|solve|sort)\b|\bhow to (?:fix|solve)\b|\bsteps to (?:fix|solve)\b|"
                 r"\bfix (?:it|this|that|#?\d+)\b|\bwhat (?:do|should) (?:i|we) do\b", t):
        ids = _targets(t, w, focus, "resolve")
        return {"fix": ids[0] if ids else None}
    if m := NAV.match(t):
        dest = m.group(1).strip()
        if inc := re.search(r"(?:incident\s*)?#?\s*(\d{1,7})$", dest):
            hit = w.incident(int(inc.group(1)))
            if hit:
                return {"navigate": f"/p/{hit['project']}/i/{hit['id']}", "reply": f"Opening #{hit['id']}: {hit['title']}."}
        for words, path in PAGES.items():
            if dest == words or dest.startswith(words + " "):
                return {"navigate": path, "reply": f"Taking you to {words}."}
        key = w.current or (next(iter(w.projects)) if w.projects else None)
        for words, suffix in sorted(PROJECT_PAGES.items(), key=lambda kv: -len(kv[0])):
            if words in dest and key:
                return {"navigate": f"/p/{key}{suffix}", "reply": f"Taking you to {words}."}
    for tool, pat in VERBS:
        if not re.search(pat, t):
            continue
        if tool in ("refresh", "run_checks"):
            project = next((k for k, p in w.projects.items() if p["name"].lower().split(" (")[0] in t or k in t), None)
            return {"tool": tool, "args": {"project": project} if project else {}}
        if tool == "send_report":
            project = next((k for k, p in w.projects.items() if p["name"].lower().split(" (")[0] in t or k in t), None)
            return {"tool": tool, "args": {"project": project}}
        if tool == "unmute":
            m = re.search(r"#?(\d+)", t)
            return {"tool": tool, "args": {"mute": m.group(1) if m else "all"}}
        if tool == "mute":
            hours = _duration(t) or 4
            pipe = next((n for _, n in sorted(w.pipelines(), key=lambda kv: -len(kv[1])) if re.search(rf"(?<![\w-]){re.escape(n.lower())}(?![\w-])", t)), None)
            ids = [int(x) for x in re.findall(r"(?:#\s?|\bincident\s+)(\d{1,7})\b", t)]
            target: Any = ids[0] if ids else pipe if pipe else "project" if re.search(r"\b(everything|all alerts|the project|whole project)\b", t) \
                else (focus if focus and re.search(r"\b(it|this|that)\b", t) else None)
            if target is None:
                return {"ask": "mute"}
            return {"tool": "mute", "args": {"target": target, "hours": hours}}
        ids = _targets(t, w, focus, tool)
        if tool == "resolve":
            note = _quoted_or_after(original, r"with (?:a |the )?note|note|because|reason|since|as|,")
            note = re.sub(r"^(?:#?\d+\s*)", "", note) if note else ""
            return {"tool": tool, "args": {"incidents": ids, "note": note if len(note) > 2 else ""}}
        if tool == "comment":
            text = _quoted_or_after(original, r"saying|that says|:|—|–")
            return {"tool": tool, "args": {"incident": ids[0] if ids else None, "text": text}}
        if tool == "acknowledge":
            return {"tool": tool, "args": {"incidents": ids}}
        return {"tool": tool, "args": {"incident": ids[0] if ids else None}}
    return None


# ── the AI planner, for everything else ───────────────────────────────────────

PLANNER = """You are the Alert Bot's assistant inside a data-pipeline monitoring app. The person can ask questions or ask you
to do things. Decide which, and answer with JSON only:
{"intent": "act" | "ask" | "chat" | "ui", "reply": "one or two friendly sentences", "actions": [{"tool": "...", "args": {...}}],
 "navigate": null | "/path", "ui": null | {"do": "...", "value": "..."}}

- "ask": a question about their pipelines, incidents, runs, tables, data quality or health. Leave actions empty; the app
  answers from its records (you don't need to answer it yourself).
- "act": they want something done. Use ONLY these tools, with ids and names exactly as listed in the state:
{tools}
  Several actions are fine (e.g. acknowledge two incidents, then mute one pipeline). The person confirms each before it runs.
- "chat": greetings, thanks, or anything outside this app: reply briefly and say what you can do.
- navigate: one of {pages}, or /p/<project>/i/<id> for an incident, or /p/<project> to switch project. Only when they ask
  to see or open something.
- "ui": they want the app itself changed for them. Set "ui" to one of: {ui}.
  (e.g. "it's too bright" → {"do": "theme", "value": "dark"}; "I want the menu on the left" → {"do": "layout", "value": "side"})
- Never invent incident numbers, pipelines or projects. If it's unclear which incident they mean, ask in "reply" and
  leave actions empty.
"""


def plan_with_ai(message: str, w: World, history: list[dict[str, str]], focus: int | None) -> tuple[dict[str, Any], str] | None:
    from .llm import AIUnavailable, client, parse_json

    if not load_ai_config().ready:
        return None
    state = {
        "me": {"name": w.user.name}, "current_project": w.current, "focused_incident": focus,
        "projects": [{"key": k, "name": p["name"], "my_role": p["role"], "pipelines": p["pipelines"][:60]} for k, p in w.projects.items()],
        "active_incidents": [{k: i[k] for k in ("id", "project", "title", "severity", "status", "site", "table", "acknowledged_by", "checks")}
                             for i in w.incidents[:40]],
        "mutes": w.mutes[:20],
    }
    tools = "\n".join(f"  {name}({t['args']}): {t['does']}{' (owners only)' if t['owner'] else ''}" for name, t in TOOLS.items())
    pages = ", ".join(sorted({*PAGES.values(), *(f"/p/<project>{s}" for s in PROJECT_PAGES.values())}))
    ui = "; ".join(f"do={d} value={v}" for d, v in UI_DOES.items())
    msgs = [{"role": "system", "content": PLANNER.replace("{tools}", tools).replace("{pages}", pages).replace("{ui}", ui)},
            {"role": "system", "content": "State (JSON):\n" + json.dumps(state, default=str)[:24000]}]
    msgs += [{"role": h["role"], "content": str(h["text"])[:1500]} for h in history[-8:] if h.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": message[:2000]})
    try:
        res = client().chat(msgs, max_tokens=700, json_mode=True, temperature=0.1)
        out = parse_json(res.text)
    except (AIUnavailable, ValueError):
        return None
    except Exception:  # noqa: BLE001 - a planner hiccup never breaks the reply: the question is answered instead
        import logging

        logging.getLogger(__name__).warning("assistant planner failed", exc_info=True)
        return None
    return (out if isinstance(out, dict) else {}), res.model


# ── one turn ──────────────────────────────────────────────────────────────────


@dataclass
class Reply:
    text: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    navigate: str | None = None
    focus: int | None = None
    mode: str = "rules"
    model: str | None = None
    incidents: list[dict[str, Any]] = field(default_factory=list)  # cards to show with the answer
    ui: dict[str, str] | None = None  # a change to the app itself, done by the portal: {"do": "theme", "value": "dark"}

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "actions": self.actions, "links": self.links, "suggestions": self.suggestions[:5],
                "navigate": self.navigate, "focus": self.focus, "mode": self.mode, "model": self.model, "incidents": self.incidents[:6],
                "ui": self.ui}


def _next_steps(w: World, focus: int | None) -> list[str]:
    inc = w.incident(focus) if focus else (w.incidents[0] if w.incidents else None)
    if inc is None:
        return ["What's wrong right now?", "Run every check now", "Refresh everything", "Show me AI usage"]
    out = []
    if inc["status"] == "open":
        out.append(f"Acknowledge #{inc['id']}")
    out += [f"Resolve #{inc['id']} with a note", f"Why did #{inc['id']} happen?", f"Send #{inc['id']} to Teams"]
    if inc["site"]:
        out.append(f"Mute {inc['site']} for 2 hours")
    return out


def _brief(i: dict[str, Any]) -> dict[str, Any]:
    return {k: i[k] for k in ("id", "project", "project_name", "title", "severity", "status", "acknowledged_by", "likely_cause")}


def respond(session: Session, user: User, message: str, *, history: list[dict[str, str]] | None = None, focus: int | None = None,
            current: str | None = None) -> Reply:
    w = world_of(session, user, current)
    if not w.projects:
        return Reply("You're not on a project yet, so there's nothing for me to look at or change. Ask a project owner to add you.")
    focus = focus if focus and w.incident(focus) else None
    cmd = parse(message, w, focus)
    mode, model = "rules", None
    if cmd is None:
        planned = plan_with_ai(message, w, history or [], focus)
        if planned:
            plan, model = planned
            mode = "ai"
            if plan.get("intent") == "act" and plan.get("actions"):
                # our own words for what's proposed (a model may write "done" before anything is), and no jumping away
                # from the page while a change waits for its confirm: where it wanted to go becomes a button
                r = _act(plan["actions"], w, focus, "", mode, model, None)
                nav = plan.get("navigate")
                if isinstance(nav, str) and re.fullmatch(r"/[\w\-/?=&.]*", nav):
                    r.links.append({"label": "Open it", "to": nav})
                return r
            ui = plan.get("ui")
            if isinstance(ui, dict) and (str(ui.get("do")), str(ui.get("value"))) in UI_SAY:
                key = (str(ui["do"]), str(ui["value"]))
                return Reply(UI_SAY[key], ui={"do": key[0], "value": key[1]}, focus=focus, mode=mode, model=model)
            if plan.get("intent") == "chat":
                return Reply(str(plan.get("reply") or "")[:600] or _hello(w), suggestions=_next_steps(w, focus), focus=focus, mode=mode, model=model)
            if plan.get("navigate") and isinstance(plan["navigate"], str) and plan["navigate"].startswith("/"):
                return Reply(str(plan.get("reply") or "Here you go.")[:300], navigate=plan["navigate"], focus=focus, mode=mode, model=model)
        return _answer(session, user, message, w, focus)
    if cmd.get("say") == "hello":
        return Reply(_hello(w), suggestions=_next_steps(w, focus), focus=focus)
    if cmd.get("say") == "thanks":
        return Reply("Any time! Anything else? I can acknowledge, resolve, comment, mute, send alerts and reports, or dig into why something failed.",
                     suggestions=_next_steps(w, focus), focus=focus)
    if cmd.get("say") == "help":
        return Reply(HELP, suggestions=["What's wrong right now?", *(_next_steps(w, focus)[:3])], focus=focus)
    if cmd.get("ask") == "mute":
        return Reply("What should I mute: an incident (#12), a pipeline (e.g. pl_sql), or every alert in the project? And for how long?",
                     suggestions=[f"Mute {i['site']} for 2 hours" for i in w.incidents if i["site"]][:3] + ["Mute everything for 1 hour"], focus=focus)
    if cmd.get("ui"):
        return Reply(cmd["reply"], ui=cmd["ui"], focus=focus)
    if cmd.get("navigate"):
        return Reply(cmd["reply"], navigate=cmd["navigate"], focus=focus)
    if "fix" in cmd:
        return _fix_reply(session, w, cmd["fix"] or focus)
    return _act([cmd], w, focus, "", mode, model, None)


HELP = ("Here's what I can do, in plain words:\n"
        "• Answer: “what's wrong?”, “why did pl_sql fail?”, “is today's data ready?”, “has this happened before?”\n"
        "• Acknowledge or resolve: “ack #12”, “ack everything critical”, “resolve the pl_sql one, re-ran it”\n"
        "• Comment: “note on #12: waiting for the vendor's file”\n"
        "• Teams: “send #12 to Teams”, “send the morning report”, “mute pl_uat for 2 hours”, “unmute”\n"
        "• Mark as expected: “#5 is expected now” (the bot stops flagging that level)\n"
        "• Run: “run the checks”, “refresh everything”\n"
        "• Go anywhere: “open compute cost”, “show the pipeline report”, “take me to #12”, “switch to <project>”\n"
        "• Change the app: “classic style”, “modern style”, “dark mode”, “move the menu to the side”, “start the tour”, “full screen”\n"
        "Nothing changes until you confirm it: incidents, alerts and reports wait for your click. "
        "Moving around and changing how the app looks happen at once.")


def _hello(w: World) -> str:
    first = w.user.name.split(" ")[0]
    need = [i for i in w.incidents if i["status"] == "open"]
    if need:
        crit = sum(i["severity"] == "critical" for i in need)
        return (f"Hi {first}! {len(need)} thing{'s need' if len(need) > 1 else ' needs'} attention"
                f"{f' ({crit} critical)' if crit else ''}. The worst: #{need[0]['id']} {need[0]['title']}. Want me to acknowledge it, or tell you why it happened?")
    held = [i for i in w.incidents if i["status"] == "acknowledged"]
    if held:
        return f"Hi {first}! Nothing new needs you: {len(held)} incident{'s are' if len(held) > 1 else ' is'} being handled. Ask me anything, or tell me what to do."
    return f"Hi {first}! All clear right now. Ask me anything about your pipelines, or tell me what to do."


def _act(cmds: list[dict[str, Any]], w: World, focus: int | None, lead: str, mode: str, model: str | None, nav: Any) -> Reply:
    proposals, problems = [], []
    for c in cmds[:8]:
        tool = str(c.get("tool") or "")
        args = c.get("args") if isinstance(c.get("args"), dict) else {}
        try:
            proposals.append(propose(tool, args, w))
        except Refusal as r:
            problems.append(str(r))
        except (TypeError, ValueError):
            problems.append("I couldn't work out the details of that.")
    touched = [i for p in proposals for i in (p["args"].get("incidents") or ([p["args"]["incident"]] if p["args"].get("incident") else []))]
    new_focus = touched[0] if len(touched) == 1 else focus
    if not proposals:
        incs = [_brief(i) for i in w.incidents if i["status"] == "open"][:4] or [_brief(i) for i in w.incidents][:4]
        return Reply(" ".join(problems) or "I'm not sure what to change. Try “ack #12” or “resolve the pl_sql one”.", incidents=incs,
                     suggestions=_next_steps(w, focus), focus=focus, mode=mode, model=model)
    blocked = [p for p in proposals if not p["allowed"]]
    text = lead or ("Here's what I'll do. Confirm and it's done." if len(proposals) > 1 else
                    f"Ready to {proposals[0]['title'][0].lower() + proposals[0]['title'][1:]}. Confirm and it's done.")
    if problems:
        text += " " + " ".join(problems)
    if blocked and len(blocked) == len(proposals):
        text = blocked[0]["why_not"] or text
    return Reply(text, actions=proposals, focus=new_focus, mode=mode, model=model,
                 navigate=nav if isinstance(nav, str) and nav.startswith("/") else None,
                 incidents=[_brief(w.incident(i)) for i in touched[:4] if w.incident(i)])


def _answer(session: Session, user: User, message: str, w: World, focus: int | None) -> Reply:
    from .ask import answer

    a = answer(session, user, message)
    refs = [int(m.group(1)) for link in a.links if (m := re.search(r"/i/(\d+)$", link.get("to", "")))]
    new_focus = refs[0] if refs and w.incident(refs[0]) else focus
    shown = [_brief(i) for i in w.incidents if i["id"] in refs][:3]
    return Reply(a.text, links=a.links, suggestions=_next_steps(w, new_focus), focus=new_focus, mode=a.mode, model=a.model or None,
                 incidents=shown)


def _fix_reply(session: Session, w: World, iid: int | None) -> Reply:
    """'How do I fix #19?': the first approach's steps, and where to say whether it worked."""
    from .fixes import quick_fix

    inc_brief = w.incident(iid) if iid else None
    if inc_brief is None:
        return Reply("Which incident? Tell me its number, e.g. “how do I fix #12?”.", suggestions=_next_steps(w, None))
    inc = session.get(Incident, iid)
    q = quick_fix(session, inc)
    link = {"label": "Open the step-by-step fix", "to": f"/p/{inc_brief['project']}/i/{iid}#fix"}
    if q is None:
        return Reply(f"#{iid} isn't a failed run, so there's no error to fix: it's about {', '.join(inc_brief['checks'])}. "
                     f"Ask me why it happened, or mark it as expected if that's fine.", links=[link], focus=iid,
                     suggestions=[f"Why did #{iid} happen?", f"#{iid} is expected now"])
    if not q["known"]:
        return Reply(f"#{iid} failed with an error my library doesn't know yet. Open the fix and I'll write steps for it; tell me if they worked "
                     f"and I'll remember.", links=[link], focus=iid)
    lines = []
    for n, step in enumerate(q["steps"], 1):
        lines.append(f"{n}. {step['do']}")
        if step.get("code"):
            lines.append(f"   {step['code']}")
    proof = f" It has worked {q['worked']} time{'s' if q['worked'] != 1 else ''} for your team." if q["worked"] else ""
    text = (f"{q['title']}. {q['meaning']}\n\nFirst approach: {q['approach']}.{proof}\n" + "\n".join(lines)
            + "\n\nWhen you've tried it, tell me on the incident whether it worked; if not, I'll try a different way.")
    return Reply(text, links=[link], focus=iid, suggestions=[f"Resolve #{iid}: fixed it", f"Why did #{iid} happen?"])
