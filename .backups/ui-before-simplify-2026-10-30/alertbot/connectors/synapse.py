"""Azure Synapse Analytics pipelines: the same pipeline / run / activity-run model as Data Factory, served by the
workspace's own endpoint (https://<workspace>.dev.azuresynapse.net). Read-only.

Reading it needs a Synapse role on the workspace (e.g. Synapse Artifact User / Monitoring Operator) on top of seeing
the workspace in Azure; without one the project shows the access error instead of runs.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from ..config import PipelineConfig
from .adf import AdfPipeline
from .base import PipelineRun

SYNAPSE_API = "2020-12-01"


class SynapsePipeline(AdfPipeline):
    scope = "https://dev.azuresynapse.net/.default"
    api_version = SYNAPSE_API
    service = "Synapse workspace"

    def __init__(self, cfg: PipelineConfig, *, credential: Any = None, transport: httpx.BaseTransport | None = None):
        super().__init__(cfg, credential=credential, transport=transport)
        self.factory_id = (f"/subscriptions/{cfg.subscription_id}/resourceGroups/{cfg.resource_group}"
                           f"/providers/Microsoft.Synapse/workspaces/{cfg.workspace}")
        self.name = cfg.workspace
        self._base = f"https://{cfg.workspace}.dev.azuresynapse.net"
        self._pipeline_of: dict[str, str] = {}

    def monitor_url(self, run_id: str) -> str:
        return f"https://web.azuresynapse.net/en/monitoring/pipelineruns/{run_id}?workspace={quote(self.factory_id, safe='')}"

    def query_runs(self, since: Any, until: Any, pipelines: list[str] | None = None) -> list[PipelineRun]:
        runs = super().query_runs(since, until, pipelines)
        self._pipeline_of.update({r.run_id: r.pipeline for r in runs})
        return runs

    def _activity_runs_url(self, run_id: str) -> str:
        # Synapse files activity runs under their pipeline
        pipeline = self._pipeline_of.get(run_id)
        if pipeline is None:
            pipeline = self._call("GET", f"{self._base}/pipelineruns/{run_id}").get("pipelineName", "")
            self._pipeline_of[run_id] = pipeline
        return f"{self._base}/pipelines/{quote(pipeline, safe='')}/pipelineruns/{run_id}/queryActivityruns"

