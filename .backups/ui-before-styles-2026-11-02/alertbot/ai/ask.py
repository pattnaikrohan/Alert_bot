"""Ask the bot: plain-language questions answered from the bot's own records.

Read-only and scoped to the asker's projects. Without an AI model it answers with rules: it recognises what the
question is about (a site, a project, a time window) and what kind of question it is (why, what's wrong, slowest,
failures, volume, health, happened before), then answers from incidents, runs and metrics, with links to the evidence.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import load_projects
from ..db import Incident, MetricObservation, Project, Run, User, UserProject
from ..engine.checks import SEVERITY_RANK
from ..settings import get_settings
from ..timeutil import utcnow

ACTIVE = ("open", "acknowledged")


@dataclass
class Answer:
    text: str
    links: list[dict[str, str]] = field(default_factory=list)
    mode: str = "rules"
    topic: str = ""
    model: str = ""


@dataclass
class Scope:
    projects: dict[int, Project]
    project: Project | None
    site: str | None
    start: datetime
    end: datetime
    window: str


# ── formatting ────────────────────────────────────────────────────────────────


def _ago(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "never"
    s = (now - at).total_seconds()
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{round(s / 60)} min ago"
    if s < 86400 * 2:
        return f"{round(s / 3600)} h ago"
    return f"{round(s / 86400)} days ago"


def _local(at: datetime) -> str:
    return at.replace(tzinfo=UTC).astimezone(get_settings().tz).strftime("%a %d %b %H:%M")


def _mins(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    return f"{seconds / 60:.0f} min" if seconds >= 90 else f"{seconds:.0f} s"


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return m.group(1) if m else text


def _link(project: Project, incident: Incident) -> dict[str, str]:
    return {"label": f"Open “{incident.title}”", "to": f"/p/{project.key}/i/{incident.id}"}


# ── understanding the question ────────────────────────────────────────────────


def _window(q: str, now: datetime) -> tuple[datetime, datetime, str]:
    tz = get_settings().tz
    local_now = now.replace(tzinfo=UTC).astimezone(tz)
    midnight = datetime.combine(local_now.date(), time(), tzinfo=tz)

    def utc(d: datetime) -> datetime:
        return d.astimezone(UTC).replace(tzinfo=None)

    if m := re.search(r"last (\d+) (hour|hours|day|days|week|weeks)", q):
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(hours=n) if unit.startswith("hour") else timedelta(days=n * (7 if unit.startswith("week") else 1))
        return now - delta, now, f"in the last {n} {unit}"
    if "yesterday" in q:
        return utc(midnight - timedelta(days=1)), utc(midnight), "yesterday"
    if "today" in q or "this morning" in q or "tonight" in q:
        return utc(midnight), now, "today"
    if "last week" in q:
        monday = midnight - timedelta(days=local_now.weekday())
        return utc(monday - timedelta(days=7)), utc(monday), "last week"
    if "this week" in q:
        monday = midnight - timedelta(days=local_now.weekday())
        return utc(monday), now, "this week"
    if "this month" in q or "last 30" in q or "month" in q:
        return now - timedelta(days=30), now, "in the last 30 days"
    return now - timedelta(days=7), now, "in the last 7 days"


def _scope(session: Session, user: User, q: str, now: datetime) -> Scope:
    configured = load_projects()
    projects = {
        p.id: p for p in session.scalars(
            select(Project).join(UserProject, UserProject.project_id == Project.id)
            .where(UserProject.user_id == user.id, Project.active.is_(True))
        ) if p.key in configured
    }
    project = next((p for p in projects.values() if p.display_name.lower() in q or re.search(rf"\b{re.escape(p.key)}\b", q)), None)
    sites = session.scalars(
        select(Run.site).where(Run.project_id.in_(projects or [-1]), Run.requested_at >= now - timedelta(days=60)).distinct()
    ).all()
    site = max((s for s in sites if s.lower() in q), key=len, default=None)
    if site and project is None:
        pid = session.scalar(select(Run.project_id).where(Run.project_id.in_(projects), Run.site == site).limit(1))
        project = projects.get(pid) if pid else None
    start, end, label = _window(q, now)
    return Scope(projects, project, site, start, end, label)


def _intent(q: str) -> str:
    rules = [
        ("hello", r"^(hi|hello|hey|thanks|thank you)\b"),
        ("ready", r"\b(ready|arrived|delivered|landed|complete|up to date|refreshed)\b"),
        ("help", r"\b(help|what can you|how do i use|what do you do)\b"),
        ("history", r"\b(before|again|history|last time|previous(ly)?|recurring|how often)\b"),
        ("why", r"\b(why|cause|caused|what happened|went wrong|explain|reason)\b"),
        ("slow", r"\b(slow|slowest|fastest|duration|took|long(est)?)\b"),
        ("rows", r"\b(rows?|volume|records?|how many|data loaded|count)\b"),
        ("failures", r"\b(fail|failed|failing|failures|errors?)\b"),
        ("problems", r"\b(wrong|problems?|issues?|incidents?|alerts?|worst|urgent|broken|attention|open)\b"),
        ("health", r"\b(health|healthy|status|how is|how's|how are|ok|okay|fine|working)\b"),
    ]
    return next((name for name, pattern in rules if re.search(pattern, q)), "unknown")


# ── answers ───────────────────────────────────────────────────────────────────


def _pids(s: Scope) -> list[int]:
    return [s.project.id] if s.project else list(s.projects)


def _active(session: Session, s: Scope) -> list[Incident]:
    rows = list(session.scalars(select(Incident).where(Incident.project_id.in_(_pids(s)), Incident.status.in_(ACTIVE))))
    if s.site:
        rows = [i for i in rows if i.scope == f"site:{s.site}"]
    rows.sort(key=lambda i: i.opened_at, reverse=True)
    rows.sort(key=lambda i: -SEVERITY_RANK.get(i.severity, 0))
    return rows


def _problems(session: Session, s: Scope, now: datetime) -> Answer:
    rows = _active(session, s)
    where = s.site or (s.project.display_name if s.project else "your projects")
    if not rows:
        return Answer(f"Nothing is wrong in {where} right now: no open incidents. I check again every few minutes.", topic="problems")
    crit = sum(i.severity == "critical" for i in rows)
    head = f"{len(rows)} open incident{'s' if len(rows) > 1 else ''} in {where}" + (f", {crit} critical" if crit else "") + ". Worst first:"
    lines = [head]
    for i in rows[:5]:
        p = s.projects[i.project_id]
        lines.append(f"• {i.title} ({p.display_name}, {i.severity}, {_ago(i.opened_at, now)}): {_first_sentence(i.likely_cause)}")
    if len(rows) > 5:
        lines.append(f"…and {len(rows) - 5} more on the Incidents page.")
    return Answer("\n".join(lines), [_link(s.projects[rows[0].project_id], rows[0])], topic="problems")


def _why(session: Session, s: Scope, now: datetime) -> Answer:
    rows = _active(session, s)
    if not rows:
        q = select(Incident).where(Incident.project_id.in_(_pids(s)), Incident.opened_at >= s.start, Incident.opened_at < s.end)
        past = list(session.scalars(q.order_by(Incident.opened_at.desc())))
        if s.site:
            past = [i for i in past if i.scope == f"site:{s.site}"]
        rows = past
    if not rows:
        failed = _failed_runs(session, s)
        if failed:
            r = failed[0]
            return Answer(f"I have no incident for {r.site} {s.window}, but its run at {_local(r.requested_at)} failed: "
                          f"{(r.error or 'no error text recorded')[:300]}", topic="why")
        what = s.site or (s.project.display_name if s.project else "your projects")
        return Answer(f"I don't see a problem with {what} {s.window}: "
                      "no incidents and no failed runs.", topic="why")
    i = rows[0]
    p = s.projects[i.project_id]
    state = "still open" if i.status in ACTIVE else f"resolved {_ago(i.resolved_at, now)}"
    lines = [f"{i.title} ({p.display_name}, {i.severity}, opened {_ago(i.opened_at, now)}, {state}).",
             f"Likely cause: {i.likely_cause}"]
    if i.impact:
        lines.append(f"Impact: {i.impact}")
    if i.suggested_action:
        lines.append(f"What to do: {i.suggested_action}")
    for e in (i.evidence or [])[:3]:
        lines.append(f"• {e.get('label')}: {e.get('detail')}")
    return Answer("\n".join(lines), [_link(p, i)], topic="why")


def _history(session: Session, s: Scope, now: datetime) -> Answer:
    target = _active(session, s)
    scope = f"site:{s.site}" if s.site else (target[0].scope if target else None)
    pids = _pids(s)
    if scope is None:
        n = session.scalar(select(Incident.id).where(Incident.project_id.in_(pids)).limit(1))
        return Answer("Tell me which site or table you mean, for example “Has Site A failed before?”." if n else
                      "I have no incidents on record yet.", topic="history")
    rows = list(session.scalars(select(Incident).where(Incident.project_id.in_(pids), Incident.scope == scope)
                                .order_by(Incident.opened_at.desc()).limit(20)))
    name = scope.split(":", 1)[-1]
    past = [i for i in rows if i.status == "resolved"]
    if not past:
        return Answer(f"This is the first time I've seen a problem with {name}: no earlier incidents on record.", topic="history")
    durations = [(i.resolved_at - i.opened_at).total_seconds() for i in past if i.resolved_at]
    lines = [f"{name} has had {len(past)} earlier incident{'s' if len(past) > 1 else ''}"
             + (f"; they usually cleared within {_mins(statistics.median(durations))}." if durations else ".")]
    for i in past[:4]:
        took = f", fixed after {_mins((i.resolved_at - i.opened_at).total_seconds())}" if i.resolved_at else ""
        lines.append(f"• {_local(i.opened_at)}: {i.title}{took}. {_first_sentence(i.likely_cause)}")
    return Answer("\n".join(lines), [_link(s.projects[past[0].project_id], past[0])], topic="history")


def _runs(session: Session, s: Scope) -> list[Run]:
    q = select(Run).where(Run.project_id.in_(_pids(s)), Run.requested_at >= s.start, Run.requested_at < s.end)
    if s.site:
        q = q.where(Run.site == s.site)
    return list(session.scalars(q.order_by(Run.requested_at.desc())))


def _failed_runs(session: Session, s: Scope) -> list[Run]:
    return [r for r in _runs(session, s) if r.run_status in ("Failed", "Cancelled") or r.trigger_status == "failed"]


def _slow(session: Session, s: Scope, _now: datetime) -> Answer:
    by: dict[str, list[float]] = defaultdict(list)
    for r in _runs(session, s):
        if r.duration_s:
            by[r.site].append(r.duration_s)
    if not by:
        return Answer(f"I have no finished runs with a duration {s.window}.", topic="slow")
    ranked = sorted(by.items(), key=lambda kv: -statistics.mean(kv[1]))
    lines = [f"Slowest {'runs' if s.site else 'sites'} {s.window}:"]
    for site, d in ranked[:4]:
        lines.append(f"• {site}: average {_mins(statistics.mean(d))}, longest {_mins(max(d))} ({len(d)} runs)")
    if len(ranked) > 1:
        site, d = ranked[-1]
        lines.append(f"Fastest: {site}, average {_mins(statistics.mean(d))}.")
    return Answer("\n".join(lines), topic="slow")


def _failures(session: Session, s: Scope, _now: datetime) -> Answer:
    runs = _runs(session, s)
    failed = [r for r in runs if r.run_status in ("Failed", "Cancelled") or r.trigger_status == "failed"]
    where = s.site or (s.project.display_name if s.project else "your projects")
    if not failed:
        return Answer(f"No failed runs in {where} {s.window} ({len(runs)} runs checked).", topic="failures")
    by: dict[str, list[Run]] = defaultdict(list)
    for r in failed:
        by[r.site].append(r)
    lines = [f"{len(failed)} of {len(runs)} runs failed in {where} {s.window}:"]
    for site, rs in sorted(by.items(), key=lambda kv: -len(kv[1]))[:5]:
        lines.append(f"• {site}: {len(rs)} failed, latest {_local(rs[0].requested_at)}: {(rs[0].error or 'no error text')[:160]}")
    return Answer("\n".join(lines), topic="failures")


def _rows(session: Session, s: Scope, _now: datetime) -> Answer:
    q = select(MetricObservation).where(MetricObservation.project_id.in_(_pids(s)), MetricObservation.metric == "row_count",
                                        MetricObservation.observed_at >= s.end - timedelta(days=30), MetricObservation.observed_at < s.end)
    if s.site:
        q = q.where(MetricObservation.site == s.site)
    by: dict[str, list[MetricObservation]] = defaultdict(list)
    for m in session.scalars(q.order_by(MetricObservation.observed_at.desc())):
        by[m.site].append(m)
    if not by:
        return Answer("I haven't measured any row counts there yet.", topic="rows")
    lines = ["Latest row counts against each site's usual volume:" if not s.site else f"{s.site}: latest runs against its usual volume:"]
    for site, ms in sorted(by.items())[:8]:
        latest, usual = ms[0].value, statistics.median([m.value for m in ms[1:]] or [ms[0].value])
        diff = (latest - usual) / usual * 100 if usual else 0
        flag = "  ⚠ unusually low" if diff < -40 else "  ⚠ unusually high" if diff > 60 else ""
        lines.append(f"• {site}: {latest:,.0f} rows at {_local(ms[0].observed_at)} (usual ~{usual:,.0f}, {diff:+.0f}%){flag}")
        if s.site:
            lines.extend(f"   {_local(m.observed_at)}: {m.value:,.0f}" for m in ms[1:5])
    return Answer("\n".join(lines), topic="rows")


def _health(session: Session, s: Scope, now: datetime) -> Answer:
    from ..api.views import health_of, last_ok_cycle

    lines = []
    for p in ([s.project] if s.project else list(s.projects.values())):
        h = health_of(last_ok_cycle(session, p.id))
        active = [i for i in session.scalars(select(Incident).where(Incident.project_id == p.id, Incident.status.in_(ACTIVE)))]
        crit = sum(i.severity == "critical" for i in active)
        runs = list(session.scalars(select(Run).where(Run.project_id == p.id, Run.requested_at >= s.start, Run.requested_at < s.end)))
        failed = sum(r.run_status in ("Failed", "Cancelled") or r.trigger_status == "failed" for r in runs)
        opened = session.scalars(select(Incident.id).where(Incident.project_id == p.id, Incident.opened_at >= s.start,
                                                           Incident.opened_at < s.end)).all()
        state = (f"{len(active)} open incident{'s' if len(active) != 1 else ''}" + (f" ({crit} critical)" if crit else "")) if active else "no open incidents"
        lines.append(f"• {p.display_name}: {f'{h:.0f}% of checks passing' if h is not None else 'not checked yet'}, {state}. "
                     f"{s.window[0].upper() + s.window[1:]}: {len(runs)} runs, {failed} failed, {len(opened)} incident{'s' if len(opened) != 1 else ''} opened.")
    if not lines:
        return Answer("You're not on any project yet, so I have nothing to report.", topic="health")
    return Answer(("Here's how things look:\n" if len(lines) > 1 else "") + "\n".join(lines), topic="health")


def _ready(session: Session, s: Scope, now: datetime) -> Answer:
    """Is today's data ready? Table by table where we know the tables (Data Factory projects), else per project."""
    from ..api.views import management_view

    cfgs = load_projects()
    lines: list[str] = []
    links: list[dict[str, str]] = []
    worst = "ok"
    for p in ([s.project] if s.project else list(s.projects.values())):
        cfg = cfgs.get(p.key)
        if cfg is None:
            continue
        m = management_view(session, p, cfg, days=7)
        if m["tables"]:
            mark = {"ok": "✅", "unusual": "⚠️", "late": "⚠️", "issue": "❌"}
            groups: dict[str, list[str]] = defaultdict(list)
            for t in m["tables"]:
                state = t["state"]
                note = {"ok": "loaded", "unusual": "loaded, unusual volume", "late": f"no load for {t['age_hours']:.0f} h",
                        "issue": "open incident"}[state]
                dq = t.get("quality")
                if dq and dq["status"] == "fail":  # arrived, but the data inside fails its checks
                    note += f", data checks failing ({', '.join(dq['failing'][:3]).lower()})"
                    state = "issue" if state == "ok" or state == "unusual" else state
                elif dq and dq["status"] == "ok":
                    note += ", data checks pass"
                groups[t["name"]].append(f"{t['env'] or ''} {mark[state]} {note}".strip())
                t["_state"] = state
            lines.append(f"{p.display_name}:")
            lines += [f"• {name}: " + "; ".join(v) for name, v in groups.items()]
            states = {t["_state"] for t in m["tables"]}
            worst = "issue" if "issue" in states else "late" if "late" in states or "unusual" in states else worst
        else:
            lvl = m["status"]["level"]
            lines.append(f"• {p.display_name} {'✅' if lvl == 'ok' else '⚠️' if lvl == 'warning' else '❌'} {m['status']['headline']}")
            worst = "issue" if lvl == "critical" else "late" if lvl == "warning" and worst == "ok" else worst
        if m["incidents"]:
            links.append({"label": f"Open “{m['incidents'][0]['title']}”", "to": f"/p/{p.key}/i/{m['incidents'][0]['id']}"})
    overall = {"ok": "Ready ✅", "late": "Partially ready ⚠️", "issue": "Not fully ready ❌"}[worst]
    return Answer("\n".join([*lines, f"Overall: {overall}"]), links[:1], topic="ready")


HELP = ("I can answer questions about your projects' data from what I've checked. Try:\n"
        "• “What's wrong right now?”\n• “Why did Site B fail?”\n• “Has Retailer South had this before?”\n"
        "• “Which sites were slowest this week?”\n• “Any failed runs yesterday?”\n• “How many rows did Site A load?”\n"
        "• “How healthy is CSDW?”\n• “Is today's data ready?”")


CORTEX_Q = re.compile(r"\b(cortex|snowflake (?:ai|intelligence)|ai_complete)\b", re.I)
COMPUTE_Q = re.compile(r"\b(snowflake|compute|credits?|warehouse (?:cost|spend|bill)|spend(?:ing)?|bill)\b", re.I)


def answer(session: Session, user: User, question: str) -> Answer:
    """The rules answer first (always available); with AI on, a model answers from the same facts, checked."""
    if CORTEX_Q.search(question):
        from ..dq.cortex import explain as cortex_explain

        if text := cortex_explain(session, user.id):
            return Answer(text, links=[{"label": "Open Cortex AI usage", "to": "/cortex"}], mode="rules", topic="cortex")
    if COMPUTE_Q.search(question) and not re.search(r"\b(ai|tokens?|openai|anthropic|llm)\b", question, re.I):
        from ..dq.compute import explain_change

        if text := explain_change(session, user):
            return Answer(text, links=[{"label": "Open Compute cost", "to": "/compute"}], mode="rules", topic="compute")
    base = _rule_answer(session, user, question)
    if base.topic in ("none", "hello", "help"):
        return base
    try:
        return _ai_answer(session, user, question, base)
    except Exception as exc:  # noqa: BLE001 - the rules' answer stands
        import logging

        logging.getLogger(__name__).info("AI answer unavailable, using the rule-based one: %s", exc)
        return base


AI_PROMPT = """You are the alert bot for data pipelines. Answer the user's question about their data in plain language,
using ONLY the facts provided (a JSON snapshot of their projects and a first answer worked out by rules).
- 2-6 short sentences, or a short list when listing tables/pipelines. Lead with the direct answer.
- Copy numbers, names and times exactly from the facts. Never invent anything; if the facts don't answer it, say what
  you do know and what's missing.
- No markdown headings, no links.
Return JSON: {"answer": "..."}"""


def _ai_answer(session: Session, user: User, question: str, base: Answer) -> Answer:
    import json

    from ..api.views import management_view
    from ..config import load_ai_config
    from .llm import client, invented_numbers

    if not load_ai_config().ready:
        return base
    s = _scope(session, user, " ".join(question.lower().split()), utcnow())
    cfgs = load_projects()
    facts: dict[str, Any] = {"first_answer_by_rules": base.text, "projects": []}
    for p in ([s.project] if s.project else list(s.projects.values()))[:4]:
        if (cfg := cfgs.get(p.key)) is None:
            continue
        m = management_view(session, p, cfg, days=14)
        facts["projects"].append({
            "name": p.display_name, "status": m["status"], "kpis": m["kpis"], "pipelines": m["pipelines"][:8],
            "data_health_score": m.get("health"), "data_quality": m.get("quality"),
            "tables": m["tables"][:40], "open_incidents": [{k: i[k] for k in ("title", "severity", "opened_at", "likely_cause")}
                                                            for i in m["incidents"][:6]],
            "failure_reasons": m["failure_reasons"][:5]})
    evidence = json.dumps(facts, default=str, ensure_ascii=False)
    llm = client()
    out, res = llm.chat_json(AI_PROMPT, f"Question: {question}\nFacts (JSON):\n{evidence[:60000]}", max_tokens=1200)
    text = str(out.get("answer") or "").strip()
    if len(text) < 10:
        raise ValueError("empty answer")
    if made_up := invented_numbers(text, evidence):
        raise ValueError(f"numbers not in the facts: {sorted(made_up)[:5]}")
    return Answer(text, base.links, mode="ai", topic=base.topic, model=res.model)


def _rule_answer(session: Session, user: User, question: str) -> Answer:
    now = utcnow()
    q = " ".join(question.lower().split())
    s = _scope(session, user, q, now)
    if not s.projects:
        return Answer("You're not on a project yet, so I can't look at any data for you. Ask a project owner to add you.", topic="none")
    intent = _intent(q)
    if intent == "hello":
        return Answer(f"Hi {user.name.split(' ')[0]}! Ask me anything about your projects' runs, incidents and data. " + HELP.split("\n", 1)[1].replace("\n", " "), topic="hello")
    if intent == "help":
        return Answer(HELP, topic="help")
    if intent == "unknown":
        if s.site or s.project:
            return _why(session, s, now) if _active(session, s) else _health(session, s, now)
        base = _problems(session, s, now)
        return Answer("I'm not sure I understood that, so here's what's going on. " + base.text +
                      "\n\nYou can also ask why something failed, what was slowest, or whether it happened before.", base.links, topic="unknown")
    return {"why": _why, "history": _history, "slow": _slow, "failures": _failures, "rows": _rows,
            "problems": _problems, "health": _health, "ready": _ready}[intent](session, s, now)


def summary(session: Session, user: User) -> Answer:
    """What the bot says for 'read me a summary': health of every project, then the worst problems."""
    now = utcnow()
    s = _scope(session, user, "", now)
    if not s.projects:
        return Answer("You're not on a project yet.", topic="summary")
    health = _health(session, Scope(s.projects, None, None, now - timedelta(days=1), now, "in the last 24 hours"), now)
    problems = _problems(session, s, now)
    return Answer(health.text + "\n" + problems.text, problems.links, topic="summary")

