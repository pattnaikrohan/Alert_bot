"""Azure Logic Apps (Consumption): every workflow in a resource group, read-only through Azure Resource Manager.

Mapped onto the same model as Data Factory so everything above it (runs, failure trees, the branch graph, learned
durations, missed schedules, incidents) works unchanged:

    workflow            → pipeline
    workflow run        → pipeline run      (Running → InProgress, Waiting → Queued, Aborted / TimedOut → Failed)
    run action          → activity run
    Recurrence trigger  → schedule
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from ..config import PipelineConfig
from .adf import ARM, AdfAccessError, AdfPipeline, Schedule, _dt, _trigger_interval
from .base import ActivityRun, PipelineRun

LOGIC_API = "2016-06-01"
STATUS = {"Running": "InProgress", "Waiting": "Queued", "Succeeded": "Succeeded", "Failed": "Failed", "Cancelled": "Cancelled",
          "Aborted": "Failed", "TimedOut": "Failed", "Suspended": "Failed", "Faulted": "Failed"}
ACTION_STATUS = {**STATUS, "Skipped": "Skipped", "Ignored": "Skipped", "Paused": "InProgress"}


class LogicAppsPipeline(AdfPipeline):
    api_version = LOGIC_API
    service = "Logic Apps"

    def __init__(self, cfg: PipelineConfig, *, credential: Any = None, transport: httpx.BaseTransport | None = None):
        super().__init__(cfg, credential=credential, transport=transport)
        self.factory_id = f"/subscriptions/{cfg.subscription_id}/resourceGroups/{cfg.resource_group}/providers/Microsoft.Logic/workflows"
        self.name = f"in {cfg.resource_group}"
        self._base = f"{ARM}{self.factory_id}"
        self._workflows: dict[str, dict[str, Any]] | None = None
        self._workflow_of: dict[str, str] = {}

    def forget(self) -> None:
        super().forget()
        self._workflows, self._workflow_of = None, {}

    # ── workflows ──
    def _all(self) -> dict[str, dict[str, Any]]:
        if self._workflows is None:
            out: dict[str, dict[str, Any]] = {}
            url: str | None = self._base
            while url:
                d = self._call("GET", url)
                for w in d.get("value", []):
                    out[w["name"]] = w.get("properties") or {}
                url = d.get("nextLink")
            self._workflows = out
        return self._workflows

    def list_pipelines(self) -> list[str]:
        return sorted(n for n, p in self._all().items() if p.get("state") != "Disabled")

    def inventory(self) -> list[dict[str, Any]]:
        skip = set(self.cfg.exclude)
        watched = set(self.cfg.pipelines) if self.cfg.pipelines else None
        out = []
        for name, props in sorted(self._all().items()):
            d = props.get("definition") or {}
            trig = []
            for tname, t in (d.get("triggers") or {}).items():
                rec = t.get("recurrence")
                every = _trigger_interval({"typeProperties": {"recurrence": rec}}) if rec else None
                kind = t.get("type", "")
                trig.append({"name": tname, "kind": {"Recurrence": "Schedule", "Request": "HTTP request", "ApiConnection": "Connector",
                                                     "ApiConnectionWebhook": "Connector webhook"}.get(kind, kind),
                             "started": props.get("state") != "Disabled",
                             "every_hours": round(every.total_seconds() / 3600, 3) if every else None})
            wid = re.search(r"/workflows/([0-9a-f]{32})", props.get("accessEndpoint") or "", re.I)
            out.append({"name": name, "type": "logic_app", "state": props.get("state") or "Enabled", "folder": None,
                        "workflow_id": wid.group(1).lower() if wid else None,
                        "description": (d.get("description") or "") if isinstance(d.get("description"), str) else "",
                        "steps": len(_flatten(_nodes(d.get("actions") or {}))), "triggers": trig,
                        "watched": props.get("state") != "Disabled" and name not in skip and (watched is None or name in watched)})
        return out

    def monitor_url(self, run_id: str) -> str:
        wf = self._workflow_of.get(run_id)
        return f"https://portal.azure.com/#resource{self.factory_id}/{wf}/logicApp" if wf else f"https://portal.azure.com/#resource{self.factory_id.rsplit('/providers', 1)[0]}"

    def schedules(self) -> dict[str, list[Schedule]]:
        out: dict[str, list[Schedule]] = {}
        for name, props in self._all().items():
            if props.get("state") == "Disabled":
                continue
            for tname, t in ((props.get("definition") or {}).get("triggers") or {}).items():
                rec = t.get("recurrence")
                every = _trigger_interval({"typeProperties": {"recurrence": rec}}) if rec else None
                if every:
                    out.setdefault(name, []).append(Schedule(tname, every, t.get("type") or "Recurrence"))
        return out

    # ── runs ──
    def _la_run(self, workflow: str, d: dict[str, Any]) -> PipelineRun | None:
        p = d.get("properties") or {}
        status = STATUS.get(p.get("status", ""))
        if status is None:  # Skipped: the trigger fired but its condition said no; not a run worth judging
            return None
        start, end = _dt(p.get("startTime")), _dt(p.get("endTime"))
        err = p.get("error") or {}
        trigger = (p.get("trigger") or {}).get("name", "")
        self._workflow_of[d["name"]] = workflow
        return PipelineRun(run_id=d["name"], pipeline=workflow, status=status, started_at=start, ended_at=end,
                           duration_s=(end - start).total_seconds() if start and end else None,
                           message=str(err.get("message") or p.get("code") or "") if status != "Succeeded" else "",
                           invoked_by=" · ".join(x for x in ("Trigger", trigger) if x) if trigger else "")

    def query_runs(self, since: datetime, until: datetime, pipelines: list[str] | None = None) -> list[PipelineRun]:
        runs: list[PipelineRun] = []
        for wf in pipelines or self.list_pipelines():
            url: str | None = f"{self._base}/{wf}/runs"
            params: dict[str, Any] | None = {"$filter": f"startTime ge {since.isoformat()}Z", "$top": 250}
            while url:
                d = self._call("GET", url, params=params)
                for x in d.get("value", []):
                    r = self._la_run(wf, x)
                    if r is not None and (r.started_at is None or r.started_at < until):
                        runs.append(r)
                url, params = d.get("nextLink"), None
        return runs

    def _workflow(self, run_id: str) -> str | None:
        if run_id in self._workflow_of:
            return self._workflow_of[run_id]
        for wf in self.list_pipelines():  # a run we haven't listed in this process: ask each workflow
            try:
                d = self._call("GET", f"{self._base}/{wf}/runs/{run_id}")
            except (AdfAccessError, httpx.HTTPStatusError):
                continue
            if d.get("name") == run_id:
                self._workflow_of[run_id] = wf
                return wf
        return None

    def get_runs(self, run_ids: list[str]) -> dict[str, PipelineRun]:
        out: dict[str, PipelineRun] = {}
        for run_id in run_ids:
            wf = self._workflow(run_id)
            if wf is None:
                continue
            r = self._la_run(wf, self._call("GET", f"{self._base}/{wf}/runs/{run_id}"))
            if r is not None:
                out[run_id] = r
        return out

    def get_activity_runs(self, run_id: str) -> list[ActivityRun]:
        wf = self._workflow(run_id)
        if wf is None:
            return []
        types = {n["name"]: n["type"] for n in _flatten(self._definition(wf))}
        acts: list[ActivityRun] = []
        url: str | None = f"{self._base}/{wf}/runs/{run_id}/actions"
        while url:
            d = self._call("GET", url)
            for a in d.get("value", []):
                p = a.get("properties") or {}
                start, end = _dt(p.get("startTime")), _dt(p.get("endTime"))
                err = p.get("error") or {}
                acts.append(ActivityRun(
                    run_id=run_id, name=a.get("name", ""), type=types.get(a.get("name", ""), ""),
                    status=ACTION_STATUS.get(p.get("status", ""), p.get("status", "")), started_at=start, ended_at=end,
                    duration_s=(end - start).total_seconds() if start and end else None, output={},
                    error={"errorCode": err.get("code") or p.get("code") or "", "message": err.get("message") or ""} if err.get("message") else None,
                ))
            url = d.get("nextLink")
        acts.sort(key=lambda a: a.started_at or datetime.min)
        return acts

    # ── definitions ──
    def _definition(self, pipeline: str) -> list[dict[str, Any]]:
        if pipeline not in self._definitions:
            props = self._all().get(pipeline)
            if props is None or "definition" not in props:
                props = self._call("GET", f"{self._base}/{pipeline}").get("properties") or {}
            self._definitions[pipeline] = _nodes((props.get("definition") or {}).get("actions") or {})
        return self._definitions[pipeline]

    def step_targets(self, pipeline: str) -> dict[str, dict[str, Any]]:  # noqa: ARG002 - Logic Apps don't load tables
        return {}

    def _linked_services(self) -> dict[str, dict[str, Any]]:
        return {}


def _nodes(actions: dict[str, Any], branch: str | None = None) -> list[dict[str, Any]]:
    out = []
    for name, a in (actions or {}).items():
        kind = a.get("type", "")
        kids = _nodes(a.get("actions") or {}, "If true" if kind == "If" else None)
        kids += _nodes(((a.get("else") or {}).get("actions")) or {}, "If false")
        for case in (a.get("cases") or {}).values():
            kids += _nodes(case.get("actions") or {}, f"Case {case.get('case')}")
        kids += _nodes(((a.get("default") or {}).get("actions")) or {}, "Default")
        out.append({"name": name, "type": kind, "branch": branch, "calls": None, "reads": [], "writes": [],
                    "depends_on": [{"activity": k, "conditions": v or ["Succeeded"]} for k, v in (a.get("runAfter") or {}).items()],
                    "children": kids})
    return out


def _flatten(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [x for n in nodes for x in [n, *_flatten(n["children"])]]
