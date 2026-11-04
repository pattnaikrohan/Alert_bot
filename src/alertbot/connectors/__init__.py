"""Connector registry: turns a project's config into live adapters."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import PipelineConfig, ProjectConfig
from .adf import AdfPipeline, FakeAdfPipeline
from .base import DataSource, Notifier, Pipeline
from .notifiers import build_notifier
from .sql_source import SqlDataSource


@dataclass
class ProjectConnectors:
    sources: dict[str, DataSource]
    pipeline: Pipeline
    notifier: Notifier

    def source(self, source_id: str) -> DataSource:
        return self.sources[source_id]


def azure_pipeline(cfg: PipelineConfig) -> AdfPipeline:
    """The read-only client for an Azure pipeline service: Data Factory, Synapse pipelines or Logic Apps."""
    if cfg.type == "synapse":
        from .synapse import SynapsePipeline

        return SynapsePipeline(cfg)
    if cfg.type == "logic_apps":
        from .logic_apps import LogicAppsPipeline

        return LogicAppsPipeline(cfg)
    return AdfPipeline(cfg)


def build_connectors(cfg: ProjectConfig) -> ProjectConnectors:
    if cfg.kind == "feeds":
        from .feeds import FeedProducers, FeedSource

        feed_sources = {s.id: FeedSource(s) for s in cfg.sources}  # type: ignore[misc]
        return ProjectConnectors(sources=feed_sources, pipeline=FeedProducers(cfg), notifier=build_notifier(cfg.alerts))  # type: ignore[arg-type]
    if cfg.kind == "adf":
        assert cfg.pipeline is not None
        return ProjectConnectors(sources={}, pipeline=azure_pipeline(cfg.pipeline), notifier=build_notifier(cfg.alerts))
    sources: dict[str, DataSource] = {s.id: SqlDataSource(s) for s in cfg.sources}
    assert cfg.pipeline is not None
    if cfg.pipeline.type == "fake_adf":
        pipeline: Pipeline = FakeAdfPipeline(cfg.pipeline, cfg.source(cfg.pipeline.source or ""))
    else:
        pipeline = AdfPipeline(cfg.pipeline)
    return ProjectConnectors(sources=sources, pipeline=pipeline, notifier=build_notifier(cfg.alerts))
