"""Pipelines nobody runs, and pipelines that run for nothing: the Data Factory / Synapse / Logic Apps side of
warehouse cost.

    unused     no run for the whole threshold, over a period the bot can see (Azure keeps run history for 45 days,
               Logic Apps 90; the bot keeps every run it has seen since). A pipeline that another pipeline in use
               calls is in use. An idle pipeline costs nothing; removing it is tidying, and one less thing to break.
    wasted     it runs, but every table it loads is one nobody reads (from the warehouse scan): the runs are paid for
               and nothing uses the result. The saving is what those runs cost (an estimate from Azure's list prices).
    watching   no run seen, but the bot hasn't seen enough time to be sure yet.

The script is Azure CLI commands (back up the definition, stop the triggers, delete): the bot never runs it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import load_projects
from ..db import BotState, Project, Run, User, UserProject
from ..timeutil import iso, utcnow

RETENTION_DAYS = {"adf": 45, "synapse": 45, "logic_apps": 90}  # how far back Azure's own run history goes
SERVICE = {"adf": "Data Factory", "synapse": "Synapse", "logic_apps": "Logic Apps"}
# Azure list prices (US): what a month of runs costs, roughly
PRICE = {
    "activity_run": 1.0 / 1000,      # orchestration: $1 per 1,000 activity runs
    "diu_hour": 0.25,                # copy data movement: $0.25 per DIU-hour on the Azure integration runtime
    "dius": 4,                       # the default for a copy
    "pipeline_hour": 0.005,          # other activities: $0.005 per hour
    "logic_action": 0.000125,        # Logic Apps Consumption: $0.000125 per standard connector action
}
PRICE_NOTE = ("Estimated from Azure list prices: $1 per 1,000 activity runs, $0.25 per DIU-hour for copies (4 DIUs), "
              "$0.005 per hour for other activities; Logic Apps $0.000125 per action. Your bill may differ.")
KEY = "storage:pipes:{}"
_SAFE = re.compile(r"^[A-Za-z0-9 _\-().]{1,140}$")


def keep_of(session: Session, project_key: str) -> dict[str, Any]:
    row = session.get(BotState, KEY.format(project_key))
    return dict((row.value or {}).get("keep") or {}) if row else {}


def save_keep(session: Session, project_key: str, keep: dict[str, Any]) -> None:
    row = session.get(BotState, KEY.format(project_key)) or BotState(key=KEY.format(project_key), value={})
    row.value, row.updated_at = {**(row.value or {}), "keep": keep}, utcnow()
    session.add(row)
    session.flush()


def _ago(d: float) -> str:
    return f"{max(1, round(d * 24))} h" if d < 1 else f"{round(d)} days" if d < 60 else f"{round(d / 30.4)} months"


def monthly_cost(service: str, runs: int, steps: int, hours: float, copies: bool) -> float:
    if service == "logic_apps":
        return runs * max(1, steps) * PRICE["logic_action"]
    move = hours * PRICE["dius"] * PRICE["diu_hour"] if copies else hours * PRICE["pipeline_hour"]
    return runs * max(1, steps) * PRICE["activity_run"] + move


def my_projects(session: Session, user: User) -> list[Project]:
    return list(session.scalars(select(Project).join(UserProject, UserProject.project_id == Project.id)
                                .where(UserProject.user_id == user.id, Project.active.is_(True)).order_by(Project.display_name)))


def _table_status(dbs: list[dict[str, Any]], conns: dict[int, Any]) -> list[tuple[Any, dict[str, str]]]:
    """(connection, {SCHEMA.TABLE: status}) for each database the person connected."""
    return [(conns[d["id"]], {r["key"].upper(): r.get("read_status") or r["status"] for r in d["tables"]}) for d in dbs if d["id"] in conns]


def pipelines_report(session: Session, user: User, days: int, dbs: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    from ..db import DbConnection
    from ..engine.adf import inventory_of
    from .service import _matches

    now = now or utcnow()
    cfgs = load_projects()
    conns = {c.id: c for c in session.scalars(select(DbConnection).where(DbConnection.user_id == user.id))}
    statuses = _table_status(dbs, conns)
    from .compute import pipeline_costs

    snow = pipeline_costs(session, user.id)  # what each pipeline's Snowflake queries cost (30 days)
    month_ago = now - timedelta(days=30)
    projects = []
    for p in my_projects(session, user):
        cfg = cfgs.get(p.key)
        if cfg is None or cfg.pipeline is None or cfg.kind != "adf":
            continue
        ptype = cfg.pipeline.type if cfg.pipeline.type in SERVICE else "adf"
        inv = inventory_of(session, p.key)
        items = inv.get("items") or []
        if not items:
            continue
        retention = RETENTION_DAYS.get(ptype, 45)
        first_run = session.scalar(select(func.min(Run.requested_at)).where(Run.project_id == p.id))
        covered = min(x for x in (p.created_at - timedelta(days=retention), first_run) if x is not None)
        last = dict(session.execute(select(Run.site, func.max(Run.requested_at)).where(Run.project_id == p.id).group_by(Run.site)).all())
        recent = session.execute(select(Run.site, func.count(), func.sum(Run.duration_s)).where(Run.project_id == p.id, Run.requested_at >= month_ago)
                                 .group_by(Run.site)).all()
        runs30 = {s: (n, (d or 0) / 3600) for s, n, d in recent}
        last_status = {}
        for s, at in last.items():
            r = session.scalar(select(Run.run_status).where(Run.project_id == p.id, Run.site == s, Run.requested_at == at).limit(1))
            last_status[s] = r
        callers: dict[str, list[str]] = {}
        for it in items:
            for c in it.get("connections") or []:
                if c.get("kind") == "pipeline" and c.get("name"):
                    callers.setdefault(c["name"], []).append(it["name"])
        trig_users: dict[str, list[str]] = {}
        for it in items:
            for t in it.get("triggers") or []:
                trig_users.setdefault(t["name"], []).append(it["name"])
        kept = keep_of(session, p.key)
        rows: dict[str, dict[str, Any]] = {}
        for it in items:
            name = it["name"]
            lr = last.get(name)
            n30, h30 = runs30.get(name, (0, 0.0))
            steps = int(it.get("steps") or 1)
            copies = any((s.get("type") == "Copy") for s in it.get("lineage") or [])
            sf = (snow.get((p.key, name)) or {}).get("monthly") or 0.0
            cost = monthly_cost(ptype, n30, steps, h30, copies) + sf
            writes = []
            for step in it.get("lineage") or []:
                for t in step.get("writes") or []:
                    if t.get("kind") != "table" or t.get("dynamic"):
                        continue
                    for conn, st in statuses:
                        if _matches(conn, t):
                            parts = str(t["table"]).upper().split(".")
                            writes.append({"table": t["table"], "status": st.get(f"{parts[1]}.{parts[2]}"), "database": conn.name})
            seen_days = (now - covered).total_seconds() / 86400
            idle = (now - lr).total_seconds() / 86400 if lr else None
            started = [t for t in it.get("triggers") or [] if t.get("started")]
            row = {
                "project": p.key, "project_name": p.display_name, "service": SERVICE[ptype], "name": name, "type": it.get("type"),
                "folder": it.get("folder"), "description": (it.get("description") or "").strip()[:300], "steps": steps,
                "enabled": it.get("state", "Enabled") != "Disabled", "triggers": it.get("triggers") or [],
                "calls": [c["name"] for c in it.get("connections") or [] if c.get("kind") == "pipeline"],
                "called_by": sorted(set(callers.get(name, []))), "last_run": iso(lr), "last_status": last_status.get(name),
                "runs_30d": n30, "hours_30d": round(h30, 2), "monthly": round(cost, 2), "yearly": round(cost * 12, 2),
                "writes": writes, "seen_days": round(seen_days, 1), "idle_days": round(idle, 1) if idle is not None else None,
                "sure_in_days": None, "saving": 0.0, "snowflake_monthly": round(sf, 2),
            }
            if name in kept:
                k = kept[name]
                row.update(status="kept", why=f"Kept by {k.get('by')}" + (f": {k['reason']}" if k.get("reason") else ""))
            elif lr is not None and idle is not None and idle < days:
                unread = [w for w in writes if w["status"] in ("unused", "write_only")]
                if writes and len(unread) == len(writes) and n30:
                    row.update(status="wasted", confidence="high", saving=round(cost, 2),
                               why=f"Runs {n30} times a month, but nobody reads what it loads ({', '.join(w['table'].split('.')[-1] for w in writes[:3])})")
                else:
                    row.update(status="in_use", why=f"Ran {_ago(idle)} ago · {n30} runs in the last 30 days")
            elif seen_days >= days:
                why = f"No run for {_ago(idle)}" if idle is not None else f"Never ran in the {_ago(seen_days)} the bot can see"
                if not started and not callers.get(name):
                    why += " · nothing starts it: no active trigger, and no other pipeline calls it"
                elif started:
                    why += f" · its trigger {started[0]['name']} is on but hasn't started it"
                row.update(status="unused", confidence="high" if not started and not callers.get(name) else "medium", why=why)
            else:
                row.update(status="watching", sure_in_days=round(days - seen_days, 1),
                           why=f"No run in the {_ago(seen_days)} the bot can see; certain in {_ago(days - seen_days)}")
            rows[name] = row
        # a pipeline in use keeps the pipelines it calls in use
        for _ in range(4):
            changed = False
            for r in rows.values():
                if r["status"] in ("unused", "watching"):
                    live = [c for c in r["called_by"] if rows.get(c, {}).get("status") in ("in_use", "wasted", "kept")]
                    if live:
                        r.update(status="in_use", why=f"Called by {live[0]}, which is in use", sure_in_days=None)
                        changed = True
            if not changed:
                break
        out = sorted(rows.values(), key=lambda r: ({"wasted": 0, "unused": 1, "watching": 2, "in_use": 3, "kept": 4}[r["status"]], -r["monthly"], r["name"].lower()))
        for r in out:
            r["shared_triggers"] = [t["name"] for t in r["triggers"] if len(trig_users.get(t["name"], [])) > 1]
        totals = {s: {"count": sum(r["status"] == s for r in out), "monthly": round(sum(r["monthly"] for r in out if r["status"] == s), 2)}
                  for s in ("wasted", "unused", "watching", "in_use", "kept")}
        totals["all"] = {"count": len(out), "monthly": round(sum(r["monthly"] for r in out), 2)}
        projects.append({"key": p.key, "name": p.display_name, "service": SERVICE[ptype], "factory": cfg.pipeline.factory or cfg.pipeline.workspace,
                         "resource_group": cfg.pipeline.resource_group, "covered_since": iso(covered), "retention_days": retention,
                         "inventory_at": inv.get("at"), "rows": out, "totals": totals})
    wasted = [r for p in projects for r in p["rows"] if r["status"] == "wasted"]
    return {"projects": projects, "price_note": PRICE_NOTE,
            "summary": {"unused": sum(p["totals"]["unused"]["count"] for p in projects), "wasted": len(wasted),
                        "watching": sum(p["totals"]["watching"]["count"] for p in projects),
                        "monthly": round(sum(r["saving"] for r in wasted), 2),
                        "run_monthly": round(sum(p["totals"]["all"]["monthly"] for p in projects), 2),
                        "pipelines": sum(len(p["rows"]) for p in projects)}}


def script(project: Project, cfg: Any, rows: list[dict[str, Any]], names: list[str], mode: str, by: str, now: datetime | None = None) -> dict[str, Any]:
    """Azure CLI commands: back up each definition, stop the triggers that start only these pipelines, then delete
    (or, mode "stop", only stop the triggers). Pipelines someone kept, or in use, are left out with a note."""
    now = now or utcnow()
    pc = cfg.pipeline
    ptype = pc.type if pc.type in SERVICE else "adf"
    by_name = {r["name"]: r for r in rows}
    chosen, skipped, warnings = [], [], []
    for n in dict.fromkeys(names):
        r = by_name.get(n)
        if r is None:
            skipped.append(f"{n}: not in this project any more")
        elif r["status"] == "kept":
            skipped.append(f"{n}: {r['why'].lower()}")
        elif r["status"] == "in_use":
            skipped.append(f"{n}: in use ({r['why']})")
        elif not _SAFE.match(n):
            skipped.append(f"{n}: its name can't be written into a command safely; remove it in the portal")
        else:
            chosen.append(r)
    picked = {r["name"] for r in chosen}
    users: dict[str, set[str]] = {}
    for r in rows:
        for t in r["triggers"]:
            users.setdefault(t["name"], set()).add(r["name"])
    triggers: dict[str, dict[str, Any]] = {}
    for r in chosen:
        for t in r["triggers"]:
            if not _SAFE.match(t["name"]):
                warnings.append(f"Trigger {t['name']} (on {r['name']}): its name can't be written into a command safely; stop it in the portal")
            elif users[t["name"]] <= picked:
                triggers[t["name"]] = t
            else:
                others = sorted(users[t["name"]] - picked)
                warnings.append(f"Trigger {t['name']} also starts {', '.join(others)}: it's left running. Remove {r['name']} from it in the "
                                f"portal first, or the delete is refused")
        for c in r["called_by"]:
            if c not in picked:
                warnings.append(f"{r['name']} is called by {c}: that pipeline fails when it runs, until its Execute Pipeline step is removed")
    sub = pc.subscription_id or "<your subscription id>"
    rg, fac, ws = pc.resource_group, pc.factory, pc.workspace
    q = lambda s: f'"{s}"'  # noqa: E731 - names are checked against _SAFE: no quotes, $ or backticks
    if ptype == "synapse":
        show = lambda n: f"az synapse pipeline show --workspace-name {q(ws)} --name {q(n)} > {q(_file(n))}"  # noqa: E731
        stop = lambda t: f"az synapse trigger stop --workspace-name {q(ws)} --name {q(t)}"  # noqa: E731
        drop_t = lambda t: f"az synapse trigger delete --workspace-name {q(ws)} --name {q(t)} --yes"  # noqa: E731
        drop = lambda n: f"az synapse pipeline delete --workspace-name {q(ws)} --name {q(n)} --yes"  # noqa: E731
        ext = None
    elif ptype == "logic_apps":
        show = lambda n: f"az logic workflow show --resource-group {q(rg)} --name {q(n)} > {q(_file(n))}"  # noqa: E731
        stop = None
        drop_t = None
        drop = lambda n: f"az logic workflow delete --resource-group {q(rg)} --name {q(n)} --yes"  # noqa: E731
        ext = "logic"
    else:
        base = f"--resource-group {q(rg)} --factory-name {q(fac)}"
        show = lambda n: f"az datafactory pipeline show {base} --name {q(n)} > {q(_file(n))}"  # noqa: E731
        stop = lambda t: f"az datafactory trigger stop {base} --name {q(t)}"  # noqa: E731
        drop_t = lambda t: f"az datafactory trigger delete {base} --name {q(t)} --yes"  # noqa: E731
        drop = lambda n: f"az datafactory pipeline delete {base} --name {q(n)} --yes"  # noqa: E731
        ext = "datafactory"
    where = f"{SERVICE[ptype]} {fac or ws or rg} (resource group {rg})"
    lines = [f"# Generated by AlertBot on {now:%Y-%m-%d %H:%M} UTC for {by}",
             f"# {where}: {len(chosen)} pipeline{'s' if len(chosen) != 1 else ''}"
             + (f", about ${sum(r['saving'] for r in chosen):,.2f} a month in runs" if any(r["saving"] for r in chosen) else ""),
             "# AlertBot never runs this. Review it, then run it yourself in Azure Cloud Shell or a terminal after `az login`,",
             "# with an account allowed to change the " + ("workspace." if ptype == "synapse" else "resource."),
             ]
    if mode == "stop":
        lines.append("# STOP ONLY: the triggers are stopped and the pipelines stay, so nothing is lost. Delete them later if nobody misses them.")
    if ext:
        lines.append(f"# Needs the Azure CLI '{ext}' extension:  az extension add --name {ext}")
    if warnings:
        lines += ["#", "# REVIEW FIRST:"] + [f"#   {w}" for w in warnings]
    if skipped:
        lines += ["#", "# Left out:"] + [f"#   {s}" for s in skipped]
    lines += ["", f"az account set --subscription {q(sub)}", ""]
    if chosen:
        lines.append("# 1. Keep a copy of each definition (to bring one back, create it again from its file)")
        lines += [show(r["name"]) for r in chosen]
        if stop and triggers:
            lines += ["", "# 2. Stop the triggers that start only these pipelines"] + [stop(t) for t in triggers]
        if ptype == "logic_apps" and mode == "stop":
            lines += ["", "# 2. Disable the workflows"] + [f"az logic workflow update --resource-group {q(rg)} --name {q(r['name'])} --state Disabled"
                                                           for r in chosen]
        if mode != "stop":
            if drop_t and triggers:
                lines += ["", "# 3. Delete those triggers"] + [drop_t(t) for t in triggers]
            lines += ["", f"# {'4' if drop_t and triggers else '3' if triggers or ptype == 'logic_apps' else '2'}. Delete the pipelines"]
            lines += [drop(r["name"]) for r in chosen]
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{project.key}-{'stop' if mode == 'stop' else 'delete'}-{len(chosen)}-pipelines-{now:%Y%m%d}").strip("-")
    return {"sql": "\n".join(lines) + "\n", "filename": f"{name}.ps1", "items": len(chosen), "tables": 0, "objects": len(chosen),
            "gb": 0.0, "monthly": round(sum(r["saving"] for r in chosen), 2), "yearly": round(sum(r["saving"] for r in chosen) * 12, 2),
            "warnings": warnings, "skipped": skipped, "mode": mode, "language": "shell"}


def _file(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") + ".json"
