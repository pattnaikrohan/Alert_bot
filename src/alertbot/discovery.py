"""Find the Azure pipelines each person can see, with their own sign-in, and watch them.

When someone signs in to the portal (and every few hours after), the bot asks Azure Resource Graph, with that person's
own token, for every Data Factory, Synapse workspace and Logic App they are allowed to see. Azure's permissions decide:

    a resource the person can see      → a project (created once, in projects/discovered/), and they're a member
    a resource they can no longer see  → their automatic membership is removed
    a resource someone already watches → they simply join it (e.g. the hand-written csdw-adf project)

Logic Apps are grouped per resource group (one project, each workflow a "pipeline"). Everything is read-only; the
monitoring of a found project uses the sign-in of the people who can see it (`pipeline.auth: signed_in_users`).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import DISCOVERED_DIR, ProjectConfig, load_projects
from .db import AzureAccess, BotState, User, UserProject, WebSession, session_scope
from .settings import get_settings
from .timeutil import iso, utcnow

log = logging.getLogger(__name__)

ARM = "https://management.azure.com"
TYPES = {"microsoft.datafactory/factories": "adf", "microsoft.synapse/workspaces": "synapse", "microsoft.logic/workflows": "logic_apps"}
LABEL = {"adf": "Data Factory", "synapse": "Synapse", "logic_apps": "Logic Apps"}
GRAPH_QUERY = ("resources | where type in~ ('microsoft.datafactory/factories', 'microsoft.synapse/workspaces', "
               "'microsoft.logic/workflows') | project id, name, type, resourceGroup, subscriptionId | order by id asc")
EVERY_HOURS = 6


@dataclass(frozen=True)
class Found:
    """One thing to watch: a factory, a Synapse workspace, or a resource group's Logic Apps."""

    kind: str  # adf | synapse | logic_apps
    subscription_id: str
    resource_group: str
    name: str  # factory / workspace name; the resource group for Logic Apps
    resource_id: str  # lower-case ARM id (the workflows collection of the resource group for Logic Apps)
    workflows: int = 0

    @property
    def label(self) -> str:
        return f"{self.name} ({LABEL[self.kind]})"


# ── asking Azure ──────────────────────────────────────────────────────────────


def find_resources(token: str, transport: httpx.BaseTransport | None = None) -> list[Found]:
    """Everything this token's owner can see, via Resource Graph (falling back to listing each subscription)."""
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=60, transport=transport, headers=headers) as c:
        try:
            rows = _graph(c)
        except Exception as exc:  # noqa: BLE001 - Resource Graph not available to this account: list per subscription
            log.info("Resource Graph unavailable (%s); listing subscriptions instead", exc)
            rows = _per_subscription(c)
    return _group(rows)


def _graph(c: httpx.Client) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    body: dict[str, Any] = {"query": GRAPH_QUERY, "options": {"resultFormat": "objectArray", "$top": 1000}}
    while True:
        r = c.post(f"{ARM}/providers/Microsoft.ResourceGraph/resources", params={"api-version": "2022-10-01"}, json=body)
        r.raise_for_status()
        d = r.json()
        rows += d.get("data") or []
        if not d.get("$skipToken"):
            return rows
        body["options"]["$skipToken"] = d["$skipToken"]


def _per_subscription(c: httpx.Client) -> list[dict[str, Any]]:
    subs: list[str] = []
    url: str | None = f"{ARM}/subscriptions?api-version=2022-12-01"
    while url:
        r = c.get(url)
        r.raise_for_status()
        subs += [s["subscriptionId"] for s in r.json().get("value", []) if s.get("state") in (None, "Enabled")]
        url = r.json().get("nextLink")
    flt = " or ".join(f"resourceType eq '{t}'" for t in ("Microsoft.DataFactory/factories", "Microsoft.Synapse/workspaces",
                                                         "Microsoft.Logic/workflows"))
    rows: list[dict[str, Any]] = []
    for sub in subs:
        url = f"{ARM}/subscriptions/{sub}/resources?api-version=2021-04-01&$filter={flt}"
        while url:
            r = c.get(url)
            if r.status_code >= 400:
                break
            for x in r.json().get("value", []):
                rg = re.search(r"/resourceGroups/([^/]+)/", x["id"], re.I)
                rows.append({"id": x["id"], "name": x["name"], "type": x["type"], "resourceGroup": rg.group(1) if rg else "",
                             "subscriptionId": sub})
            url = r.json().get("nextLink")
    return rows


def _group(rows: list[dict[str, Any]]) -> list[Found]:
    out: dict[str, Found] = {}
    workflows: dict[str, int] = {}
    for x in rows:
        kind = TYPES.get(str(x.get("type", "")).lower())
        if kind is None:
            continue
        sub, rg = x.get("subscriptionId", ""), x.get("resourceGroup", "")
        if kind == "logic_apps":
            rid = f"/subscriptions/{sub}/resourcegroups/{rg}/providers/microsoft.logic/workflows".lower()
            workflows[rid] = workflows.get(rid, 0) + 1
            out[rid] = Found("logic_apps", sub, rg, rg, rid, workflows[rid])
        else:
            rid = str(x["id"]).lower()
            out[rid] = Found(kind, sub, rg, x["name"], rid)
    return sorted(out.values(), key=lambda f: (f.kind, f.name.lower()))


# ── turning what was found into projects ──────────────────────────────────────


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "x"


def _matches(cfg: ProjectConfig, f: Found) -> bool:
    p = cfg.pipeline
    if cfg.kind != "adf" or p is None:
        return False
    if p.resource_id:
        return p.resource_id.lower() == f.resource_id
    same_place = p.subscription_id.lower() == f.subscription_id.lower() and p.resource_group.lower() == f.resource_group.lower()
    return same_place and ((f.kind == "adf" and p.type == "adf" and p.factory.lower() == f.name.lower())
                           or (f.kind == "synapse" and p.type == "synapse" and p.workspace.lower() == f.name.lower())
                           or (f.kind == "logic_apps" and p.type == "logic_apps"))


def project_for(f: Found, who: str, projects: dict[str, ProjectConfig]) -> tuple[str, bool]:
    """The project watching `f`: an existing one, or a new file in projects/discovered/. (key, created)"""
    for key, cfg in projects.items():
        if _matches(cfg, f):
            return key, False
    prefix = {"adf": "adf", "synapse": "syn", "logic_apps": "la"}[f.kind]
    key = f"{prefix}-{_slug(f.name)}"
    if key in projects:  # same name elsewhere (another subscription)
        key = f"{key}-{hashlib.sha1(f.resource_id.encode()).hexdigest()[:6]}"
    pipeline: dict[str, Any] = {"type": f.kind, "subscription_id": f.subscription_id, "resource_group": f.resource_group,
                                "resource_id": f.resource_id, "auth": "signed_in_users"}
    if f.kind == "adf":
        pipeline["factory"] = f.name
    elif f.kind == "synapse":
        pipeline["workspace"] = f.name
    where = f"resource group {f.resource_group}, subscription {f.subscription_id}"
    doc = {
        "project": key,
        "display_name": f"{f.name} ({LABEL[f.kind]})" if f.kind != "logic_apps" else f"Logic Apps · {f.name}",
        "description": {"adf": f"Azure Data Factory {f.name}", "synapse": f"Synapse workspace {f.name} pipelines",
                        "logic_apps": "Every Logic App"}[f.kind] + f" ({where}). Found in Azure automatically; read-only.",
        "kind": "adf",
        "schedule": "*/10 * * * *",
        "pipeline": pipeline,
        "alerts": {"notifier": "power_automate", "flow_url": "", "morning_report": ""},
    }
    ProjectConfig.model_validate(doc)  # never write a file the loader would reject
    folder = get_settings().config_dir / "projects" / DISCOVERED_DIR
    folder.mkdir(parents=True, exist_ok=True)
    header = (f"# Found in Azure with {who}'s sign-in on {utcnow():%Y-%m-%d %H:%M} UTC. The bot never overwrites this file:\n"
              "# edit it freely (e.g. add `pipelines:` / `exclude:`, a Teams flow_url, morning_report: \"08:00\").\n")
    (folder / f"{key}.yaml").write_text(header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return key, True


def apply(session: Session, user: User, home_account_id: str, found: list[Found]) -> dict[str, Any]:
    """Projects and memberships for what Azure showed this person; removes what it no longer shows."""
    from .engine.cycle import ensure_project

    now = utcnow()
    projects = load_projects()
    keys: dict[str, str] = {}
    created: list[str] = []
    for f in found:
        key, new = project_for(f, user.email, projects)
        keys[f.resource_id] = key
        if new:
            created.append(key)
            projects = load_projects()  # include the file just written
    admins = list(session.scalars(select(User).where(User.is_admin.is_(True))))
    joined: list[str] = []
    for f in found:
        key = keys[f.resource_id]
        project = ensure_project(session, projects[key])
        row = session.scalars(select(AzureAccess).where(AzureAccess.user_id == user.id, AzureAccess.resource_id == f.resource_id)).first()
        if row is None:
            session.add(AzureAccess(user_id=user.id, resource_id=f.resource_id, project_key=key, home_account_id=home_account_id, seen_at=now))
        else:
            row.project_key, row.home_account_id, row.seen_at = key, home_account_id, now
        if session.get(UserProject, (user.id, project.id)) is None:
            # whoever brings a resource into the bot owns it there; later people see it
            session.add(UserProject(user_id=user.id, project_id=project.id, role="owner" if key in created else "viewer"))
            joined.append(key)
        if key in created:
            for admin in admins:
                if admin.id != user.id and session.get(UserProject, (admin.id, project.id)) is None:
                    session.add(UserProject(user_id=admin.id, project_id=project.id, role="owner"))
    session.flush()

    # what Azure no longer shows this person
    removed: list[str] = []
    gone = session.scalars(select(AzureAccess).where(AzureAccess.user_id == user.id,
                                                     AzureAccess.resource_id.notin_(list(keys) or [""]))).all()
    for row in gone:
        session.delete(row)
        cfg = projects.get(row.project_key)
        still = session.scalars(select(AzureAccess.id).where(AzureAccess.user_id == user.id, AzureAccess.project_key == row.project_key,
                                                             AzureAccess.id != row.id)).first()
        if cfg is None or still is not None or user.is_admin:
            continue
        project = ensure_project(session, cfg)
        link = session.get(UserProject, (user.id, project.id))
        found_project = cfg.pipeline is not None and cfg.pipeline.auth == "signed_in_users"
        if link is not None and (found_project or link.role == "viewer"):
            session.delete(link)
            removed.append(row.project_key)
    session.flush()
    return {"found": len(found), "projects": sorted(set(keys.values())), "created": created, "joined": joined, "removed": removed}


# ── entry points ──────────────────────────────────────────────────────────────


def _set_status(session: Session, user_id: int, **value: Any) -> None:
    key = f"discovery:{user_id}"
    row = session.get(BotState, key) or BotState(key=key)
    row.value = {**(row.value or {}), **value, "at": iso(utcnow())}
    row.updated_at = utcnow()
    session.add(row)
    session.flush()


def status(session: Session, user_id: int) -> dict[str, Any] | None:
    row = session.get(BotState, f"discovery:{user_id}")
    return row.value if row else None


def discover_user(user_id: int, home_account_id: str, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Look up what this person can see in Azure (with their own sign-in) and update their projects."""
    from . import msauth

    with session_scope() as s:
        _set_status(s, user_id, status="running", message="Looking for Data Factory, Synapse and Logic Apps in Azure…")
    try:
        token, _ = msauth.user_token(home_account_id)
        found = find_resources(token, transport=transport)
        with session_scope() as s:
            user = s.get(User, user_id)
            assert user is not None
            result = apply(s, user, home_account_id, found)
            counts = {k: sum(f.kind == k for f in found) for k in LABEL}
            _set_status(s, user_id, status="done", **result, counts=counts,
                        message=_summary(counts) if found else "Azure shows you no Data Factory, Synapse workspace or Logic App yet.")
        log.info("discovery for user %s: %s", user_id, result)
        return result
    except Exception as exc:  # noqa: BLE001 - reported to the person in the portal
        log.warning("discovery for user %s failed: %s", user_id, exc)
        with session_scope() as s:
            _set_status(s, user_id, status="error", message=str(exc)[:300])
        return {"error": str(exc)}


_NOUNS = {"adf": ("Data Factory", "Data Factories"), "synapse": ("Synapse workspace", "Synapse workspaces"),
          "logic_apps": ("resource group with Logic Apps", "resource groups with Logic Apps")}


def _summary(counts: dict[str, int]) -> str:
    return "Found " + ", ".join(f"{n} {_NOUNS[k][n != 1]}" for k, n in counts.items() if n) + "."


def discover_in_background(user_id: int, home_account_id: str) -> None:
    threading.Thread(target=discover_user, args=(user_id, home_account_id), daemon=True, name=f"discover-{user_id}").start()


def rediscover_all(now: datetime | None = None, force: bool = False) -> list[int]:
    """Worker: refresh everyone's view every few hours (new resources appear, revoked access disappears)."""
    now = now or utcnow()
    with session_scope() as s:
        last = s.get(BotState, "discovery:all")
        if not force and last is not None and (now - last.updated_at).total_seconds() < EVERY_HOURS * 3600:
            return []
        people = {}
        for user_id, hid in s.execute(select(WebSession.user_id, WebSession.azure_account).where(WebSession.azure_account.is_not(None))
                                      .order_by(WebSession.last_seen_at)):
            people[user_id] = hid  # each person's most recent sign-in
        row = last or BotState(key="discovery:all")
        row.value, row.updated_at = {"users": len(people)}, now
        s.add(row)
    for user_id, hid in people.items():
        discover_user(user_id, hid)
    return list(people)
