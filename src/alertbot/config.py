"""Typed project configuration.

Each `config/projects/<key>.yaml` is validated into a `ProjectConfig` at load
time, so a typo fails loudly at start-up instead of silently skipping a check.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .settings import get_settings

log = logging.getLogger(__name__)

CHECK_NAMES = (
    "stuck_trigger",
    "run_failed",
    "run_too_slow",
    "row_count",
    "empty_site",
    "null_spike",
    "duplicates",
    "freshness",
    "schema_drift",
    "mismatch",
    # feeds projects
    "missed_run",
    "producer_failed",
    # adf projects: rows a copy step read but skipped (incompatible rows)
    "rows_skipped",
    # data quality inside the tables
    "value_range",
    "business_rule",
    "referential_integrity",
    "distribution_shift",
    # adf projects: a running pipeline that, at its current pace, will finish late
    "sla_risk",
)

Severity = Literal["info", "warning", "critical"]
FEED_OUTPUT_ID = "feed"
ADF_OUTPUT_ID = "adf"  # rows copied by an ADF pipeline run (adf projects)

# Identifiers that end up in SQL (tables, columns, task names) are validated, never free text.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}$")


def _ident(value: str, what: str) -> str:
    if not _IDENT.match(value):
        raise ValueError(f"{what} '{value}' is not a plain identifier (letters, digits, _ and $, dot-separated)")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceConfig(_Strict):
    id: str
    type: Literal["sqlite", "azure_sql", "snowflake"]
    url: str | None = None
    server: str | None = None
    database: str | None = None
    auth: Literal["managed_identity", "default_credential"] = "managed_identity"
    # snowflake (secret references: env://, kv:// or literal)
    account: str = ""  # e.g. env://SNOWFLAKE_ACCOUNT  (org-account identifier)
    user: str = ""  # e.g. env://SNOWFLAKE_USER
    role: str = ""
    warehouse: str = ""
    authenticator: Literal["externalbrowser", "snowflake_jwt"] = "externalbrowser"
    private_key: str = ""  # snowflake_jwt: PEM (kv://...) for a service user
    data_timezone: str = "UTC"  # timezone of TIMESTAMP_NTZ values written by the producers

    @model_validator(mode="after")
    def _check_target(self) -> SourceConfig:
        if self.type == "sqlite" and not self.url:
            raise ValueError(f"source '{self.id}': sqlite sources need a url")
        if self.type == "azure_sql" and not (self.url or (self.server and self.database)):
            raise ValueError(f"source '{self.id}': azure_sql needs server + database (or a url)")
        if self.type == "snowflake" and not (self.account and self.user and self.database):
            raise ValueError(f"source '{self.id}': snowflake needs account, user and database")
        return self


class FeedFilter(_Strict):
    """Rows of a shared table that belong to this feed: column ILIKE any include, and NOT ILIKE every exclude."""

    column: str
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("column")
    @classmethod
    def _col(cls, v: str) -> str:
        return _ident(v, "filter column")


class ProducerConfig(_Strict):
    """What writes the feed, so the bot can also check the producer's own health."""

    type: Literal["azure_function", "snowflake_task", "snowflake_stream", "manual"]
    name: str = ""  # Function App name / fully qualified task or stream
    app_insights_app_id: str = ""  # azure_function: Application Insights app id (enables failure checks)
    task: str = ""  # snowflake_stream: the task that runs the procedure, if any

    @model_validator(mode="after")
    def _names(self) -> ProducerConfig:
        if self.type in ("snowflake_task", "snowflake_stream") and self.name:
            _ident(self.name, "producer name")
        if self.task:
            _ident(self.task, "producer task")
        return self


class FeedConfig(_Strict):
    id: str
    name: str
    description: str = ""
    source: str
    table: str
    timestamp: str  # column holding the load / ingestion time
    timestamp_cast: Literal["none", "try_to_timestamp"] = "none"
    filter: FeedFilter | None = None
    key: list[str] = Field(default_factory=list)  # duplicates check
    required_fields: list[str] = Field(default_factory=list)  # null-spike check
    schedule: str  # cron in the project's timezone, e.g. "0 4,10,16,22 * * *"
    grace_minutes: int = 60  # rows must land within this long after a scheduled run
    # Rows only when something happens (e.g. cyclone positions): an empty run is normal, so no missed-run / volume checks.
    event_driven: bool = False
    # Freshness limit; default = two schedule intervals + grace.
    stale_after_hours: float | None = None
    producer: ProducerConfig = Field(default_factory=lambda: ProducerConfig(type="manual"))

    @model_validator(mode="after")
    def _idents(self) -> FeedConfig:
        _ident(self.table, "feed table")
        _ident(self.timestamp, "timestamp column")
        for c in [*self.key, *self.required_fields]:
            _ident(c, "column")
        if not re.match(r"^[a-z0-9_]+$", self.id):
            raise ValueError(f"feed id '{self.id}' must be lowercase letters, digits or _")
        return self


class PipelineConfig(_Strict):
    # adf: Azure Data Factory · synapse: Synapse Analytics pipelines · logic_apps: Logic Apps (Consumption) in a resource group
    type: Literal["adf", "fake_adf", "synapse", "logic_apps"]
    source: str | None = None  # fake_adf only: where the simulated run history lives
    subscription_id: str = ""
    resource_group: str = ""
    factory: str = ""
    workspace: str = ""     # synapse: the workspace name
    resource_id: str = ""   # the Azure resource this project watches (set for projects found in Azure)
    # adf projects: the pipelines to watch; empty = every pipeline in the factory (minus `exclude`)
    pipelines: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    # How the bot signs in to Azure:
    #   device_code         a person signs in once with `alertbot adf-login`; the sign-in is cached (pilot)
    #   default_credential  managed identity / service principal / az login (production)
    #   signed_in_users     the Microsoft sign-in of portal users who can see this resource in Azure (projects found
    #                       automatically); read-only, any one of them with a valid sign-in will do
    auth: Literal["device_code", "default_credential", "signed_in_users"] = "default_credential"
    tenant_id: str = ""  # device_code: the factory's Entra tenant; empty = the signed-in account's home tenant
    # adf projects: pipeline → latest local finish time ("07:00"). Without one, "late" means later than usual.
    sla: dict[str, str] = Field(default_factory=dict)

    @field_validator("sla")
    @classmethod
    def _sla_times(cls, v: dict[str, str]) -> dict[str, str]:
        bad = [k for k, t in v.items() if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t)]
        if bad:
            raise ValueError(f"sla times are local HH:MM, e.g. 07:00 (check: {', '.join(bad)})")
        return v


class TriggerStatuses(_Strict):
    pending: list[str] = Field(default_factory=lambda: ["pending"])
    running: list[str] = Field(default_factory=lambda: ["running"])
    succeeded: list[str] = Field(default_factory=lambda: ["succeeded"])
    failed: list[str] = Field(default_factory=lambda: ["failed"])

    def state_of(self, raw: str | None) -> str:
        value = (raw or "").strip().lower()
        for state in ("pending", "running", "succeeded", "failed"):
            if value in (v.lower() for v in getattr(self, state)):
                return state
        return "unknown"


class TriggerTableConfig(_Strict):
    source: str
    table: str
    id_column: str = "id"
    site_column: str = "site"
    status_column: str = "status"
    run_id_column: str = "adf_run_id"
    created_column: str = "created_at"
    statuses: TriggerStatuses = Field(default_factory=TriggerStatuses)


class OutputConfig(_Strict):
    id: str
    source: str
    table: str
    key: list[str]
    group_by: str
    loaded_at: str
    required_fields: list[str] = Field(default_factory=list)


class CheckSettings(BaseModel):
    """Per-check settings. Known keys are typed; check-specific parameters pass through."""

    model_config = ConfigDict(extra="allow", frozen=True)

    enabled: bool = True
    severity: Severity | None = None  # pin a severity instead of the computed one

    def param(self, name: str, default: Any = None) -> Any:
        return (self.model_extra or {}).get(name, default)


class AlertsConfig(_Strict):
    notifier: Literal["power_automate", "teams_workflow", "none"] = "power_automate"
    flow_url: str = ""  # secret reference: kv://..., env://..., or empty for dry-run
    teams_channel: str = ""
    email_owners_on: list[Severity] = Field(default_factory=lambda: ["critical"])
    morning_report: str = "08:00"  # local time for the Daily Morning Report; empty = off

    @field_validator("morning_report")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if v and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("morning_report is a local time like 08:00 (or empty to switch it off)")
        return v


class QualitySource(_Strict):
    """Where the tables live. Snowflake (sign-in: browser SSO for a pilot, key-pair for production), or SQLite for tests."""

    type: Literal["snowflake", "sqlite"] = "snowflake"
    account: str = ""
    user: str = ""
    role: str = ""
    warehouse: str = ""
    database: str = ""
    authenticator: Literal["externalbrowser", "snowflake_jwt"] = "externalbrowser"
    private_key: str = ""
    url: str = ""  # sqlite only


class QualityRule(_Strict):
    """A business rule: a SQL condition every row should meet, e.g. `order_amount > 0`."""

    name: str
    sql: str
    max_fail_rate: float = 0.0
    severity: Literal["warning", "critical"] = "warning"

    @field_validator("sql")
    @classmethod
    def _read_only(cls, v: str) -> str:
        if re.search(r";|--|/\*|\b(insert|update|delete|merge|drop|alter|create|truncate|grant|call|copy|put)\b", v, re.I):
            raise ValueError("a rule is a single read-only condition, e.g. order_amount > 0")
        return v


class QualityReference(_Strict):
    """Referential integrity: every non-empty `column` must exist in `table`.`ref_column`."""

    column: str
    table: str
    ref_column: str


class QualityTable(_Strict):
    table: str
    pipeline: str = ""
    date_column: str = ""            # the load-date column for "today's rows"; empty = picked automatically
    key: list[str] = Field(default_factory=list)       # duplicates by key; empty = identical rows
    required: list[str] = Field(default_factory=list)  # columns that should (almost) never be empty
    required_strict: bool = False    # any empty value in a required column is a failure
    null_tolerance: float = 0.02     # how far above its normal a column's empty share may go
    max_duplicate_rate: float = 0.001
    freshness_hours: float | None = None  # empty = learned from how often new rows usually arrive (at least 26 h)
    allow_negative: list[str] = Field(default_factory=list)
    distribution: list[str] | None = None  # text columns whose value mix is watched; None = picked automatically
    rules: list[QualityRule] = Field(default_factory=list)
    references: list[QualityReference] = Field(default_factory=list)

    @field_validator("table")
    @classmethod
    def _table(cls, v: str) -> str:
        _ident(v, "quality table")
        return v.upper()


class QualityConfig(_Strict):
    enabled: bool = True
    source: QualitySource
    auto: bool = True                # check every Snowflake table the pipelines load
    environments: list[str] = Field(default_factory=lambda: ["PROD"])
    every_hours: float = 6           # also re-check at least this often (always after a new load)
    max_columns: int = 60
    tables: list[QualityTable] = Field(default_factory=list)  # extra tables, or settings for auto-found ones

    def table(self, name: str) -> QualityTable:
        return next((t for t in self.tables if t.table == name.upper()), QualityTable(table=name))


class ProjectConfig(_Strict):
    project: str
    display_name: str
    description: str = ""
    # scrape_runs: a trigger table kicks off pipeline runs that write staging tables (the original design)
    # feeds: scheduled producers (Function Apps, Snowflake tasks/streams, scrapers) write straight into tables
    # adf: watch Azure Data Factory pipeline runs directly (each pipeline is one watched item)
    kind: Literal["scrape_runs", "feeds", "adf"] = "scrape_runs"
    timezone: str = ""  # schedule timezone for feeds; empty = ALERTBOT_TIMEZONE
    schedule: str = "*/10 * * * *"
    active: bool = True
    sources: list[SourceConfig] = Field(default_factory=list)
    pipeline: PipelineConfig | None = None
    trigger_table: TriggerTableConfig | None = None
    outputs: list[OutputConfig] = Field(default_factory=list)
    feeds: list[FeedConfig] = Field(default_factory=list)
    checks: dict[str, CheckSettings] = Field(default_factory=dict)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    quality: QualityConfig | None = None

    @field_validator("project")
    @classmethod
    def _key_format(cls, value: str) -> str:
        if not value.replace("_", "").replace("-", "").isalnum() or value.lower() != value:
            raise ValueError("project key must be lowercase letters, digits, '-' or '_'")
        return value

    @field_validator("checks")
    @classmethod
    def _known_checks(cls, value: dict[str, CheckSettings]) -> dict[str, CheckSettings]:
        unknown = set(value) - set(CHECK_NAMES)
        if unknown:
            raise ValueError(f"unknown checks: {sorted(unknown)}; known: {list(CHECK_NAMES)}")
        return value

    @model_validator(mode="after")
    def _references(self) -> ProjectConfig:
        ids = {s.id for s in self.sources}
        if self.kind == "scrape_runs":
            if not (self.pipeline and self.trigger_table and self.outputs):
                raise ValueError("scrape_runs projects need pipeline, trigger_table and outputs")
            refs = [self.trigger_table.source, *(o.source for o in self.outputs)]
            if self.pipeline.type == "fake_adf":
                refs.append(self.pipeline.source or "")
        elif self.kind == "adf":
            # the "adf" kind covers every Azure pipeline service with runs and steps: Data Factory, Synapse, Logic Apps
            p = self.pipeline
            need = {"adf": "factory", "synapse": "workspace", "logic_apps": None}
            if not (p and p.type in need and p.subscription_id and p.resource_group and (need[p.type] is None or getattr(p, need[p.type]))):
                raise ValueError("adf projects need pipeline: {type: adf, subscription_id, resource_group, factory} "
                                 "(or type: synapse with workspace, or type: logic_apps)")
            refs = []
        else:
            if not self.feeds:
                raise ValueError("feeds projects need at least one feed")
            refs = [f.source for f in self.feeds]
            dupes = {f.id for f in self.feeds if [g.id for g in self.feeds].count(f.id) > 1}
            if dupes:
                raise ValueError(f"duplicate feed ids: {sorted(dupes)}")
        missing = sorted({r for r in refs if r not in ids})
        if missing:
            raise ValueError(f"unknown source ids referenced: {missing}")
        return self

    @property
    def primary_output_id(self) -> str:
        """The output whose row counts the overview charts use (feeds share one id: each feed is a 'site')."""
        if self.outputs:
            return self.outputs[0].id
        return ADF_OUTPUT_ID if self.kind == "adf" else FEED_OUTPUT_ID

    @property
    def schema_tables(self) -> dict[str, tuple[str, str]]:
        """Schema-tracked tables: output id → (source id, table name). Feeds key each distinct table by its lowercase name."""
        if self.kind == "feeds":
            return {f.table.lower(): (f.source, f.table) for f in self.feeds}
        return {o.id: (o.source, o.table) for o in self.outputs}

    def feed_by_name(self, name: str) -> FeedConfig | None:
        return next((f for f in self.feeds if f.name == name), None)

    def source(self, source_id: str) -> SourceConfig:
        return next(s for s in self.sources if s.id == source_id)

    def check(self, name: str) -> CheckSettings:
        return self.checks.get(name, CheckSettings())


class AIConfig(_Strict):
    enabled: bool = False
    provider: Literal["", "openrouter"] = ""
    # chat models, tried in order: if one is rate-limited, down or refuses, the next one answers
    models: list[str] = Field(default_factory=list)
    decision_model: str = ""   # triage (calibrated yes/no + urgency)
    embedding_model: str = ""  # similar past incidents
    speech_model: str = ""     # the robot's voice
    api_key: str = ""
    timeout_seconds: int = 45
    # legacy single-model fields (still accepted)
    model: str = ""
    endpoint: str = ""
    framework: str = ""
    max_tool_calls: int = 8

    @property
    def chat_models(self) -> list[str]:
        return self.models or ([self.model] if self.model else [])

    @property
    def ready(self) -> bool:
        return self.enabled and self.provider == "openrouter" and bool(self.chat_models and self.api_key)


class MemberSeed(_Strict):
    email: str
    name: str
    admin: bool = False
    projects: dict[str, Literal["viewer", "owner"]] = Field(default_factory=dict)


class ConfigError(RuntimeError):
    pass


def _read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


DISCOVERED_DIR = "discovered"  # projects/discovered/*.yaml: written by the bot for resources found in Azure
_PROJECTS: dict[Path, tuple[tuple[tuple[str, int], ...], dict[str, ProjectConfig]]] = {}


def load_projects(config_dir: Path | None = None) -> dict[str, ProjectConfig]:
    """Every project: the hand-written ones (projects/*.yaml), then those found in Azure (projects/discovered/*.yaml).
    Re-read whenever a file changes, so the API and the worker pick up newly found resources without a restart."""
    root = (config_dir or get_settings().config_dir) / "projects"
    manual = sorted(root.glob("*.y*ml"))
    found = sorted((root / DISCOVERED_DIR).glob("*.y*ml"))
    sig = tuple((str(f), f.stat().st_mtime_ns) for f in manual + found)
    hit = _PROJECTS.get(root)
    if hit is not None and hit[0] == sig:
        return hit[1]
    projects: dict[str, ProjectConfig] = {}
    for path in manual + found:
        try:
            cfg = ProjectConfig.model_validate(_read_yaml(path))
        except Exception as exc:
            if path in found:  # a bad generated file must not take the hand-written projects down
                log.warning("skipping %s: %s", path, exc)
                continue
            raise ConfigError(f"{path}: {exc}") from exc
        if cfg.project in projects:
            if path in found:
                continue
            raise ConfigError(f"{path}: duplicate project key '{cfg.project}'")
        projects[cfg.project] = cfg
    _PROJECTS[root] = (sig, projects)
    return projects


load_projects.cache_clear = _PROJECTS.clear  # type: ignore[attr-defined]


@lru_cache
def load_ai_config(config_dir: Path | None = None) -> AIConfig:
    path = (config_dir or get_settings().config_dir) / "ai.yaml"
    if not path.exists():
        return AIConfig()
    return AIConfig.model_validate(_read_yaml(path).get("ai", {}))


def load_member_seed(config_dir: Path | None = None) -> list[MemberSeed]:
    path = (config_dir or get_settings().config_dir) / "members.yaml"
    if not path.exists():
        return []
    return [MemberSeed.model_validate(u) for u in _read_yaml(path).get("users", [])]
