"""Connector interfaces.

The bot's core only talks to these. A project on Synapse, Databricks or
Airflow later needs a new adapter that satisfies the same Protocol, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..config import OutputConfig, TriggerTableConfig


@dataclass(frozen=True)
class TriggerRow:
    trigger_id: str
    site: str
    raw_status: str
    state: str  # pending | running | succeeded | failed | unknown
    run_id: str | None
    requested_at: datetime


@dataclass(frozen=True)
class PipelineRun:
    run_id: str
    pipeline: str
    status: str  # Queued | InProgress | Succeeded | Failed | Cancelling | Cancelled
    started_at: datetime | None
    ended_at: datetime | None
    duration_s: float | None
    parameters: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    invoked_by: str = ""  # trigger or person that started the run


@dataclass(frozen=True)
class ActivityRun:
    run_id: str
    name: str
    type: str
    status: str
    started_at: datetime | None
    ended_at: datetime | None
    duration_s: float | None
    output: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    input: dict[str, Any] = field(default_factory=dict)  # the step's evaluated inputs (ADF), e.g. a Filter's items


@dataclass(frozen=True)
class WindowMetrics:
    row_count: int
    null_rates: dict[str, float]
    duplicate_rows: int

    @property
    def duplicate_rate(self) -> float:
        return self.duplicate_rows / self.row_count if self.row_count else 0.0


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str


class DataSource(Protocol):
    def fetch_trigger_rows(self, cfg: TriggerTableConfig, since: datetime) -> list[TriggerRow]: ...

    def window_metrics(
        self, output: OutputConfig, site: str, start: datetime, end: datetime
    ) -> WindowMetrics: ...

    def rows_by_site(self, output: OutputConfig, start: datetime, end: datetime) -> dict[str, int]: ...

    def rows_outside_windows(
        self, output: OutputConfig, site: str, start: datetime, end: datetime, windows: list[tuple[datetime, datetime]]
    ) -> int: ...

    def last_loaded_by_site(self, output: OutputConfig, since: datetime) -> dict[str, datetime]: ...

    def read_schema(self, output: OutputConfig) -> list[ColumnInfo]: ...


class Pipeline(Protocol):
    def get_runs(self, run_ids: list[str]) -> dict[str, PipelineRun]: ...

    def get_activity_runs(self, run_id: str) -> list[ActivityRun]: ...


class Notifier(Protocol):
    channel: str

    @property
    def configured(self) -> bool: ...

    def send(self, payload: dict[str, Any], idempotency_key: str) -> None: ...
