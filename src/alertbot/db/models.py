"""Metadata DB schema, shared by the bot (writer) and the API (reader).

Portable across SQLite (prototype) and Azure SQL (production): no
dialect-specific types, JSON stored via SQLAlchemy's generic JSON type.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ..timeutil import utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON, datetime: DateTime()}


# ── People and access ─────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    entra_oid: Mapped[str | None] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime | None]

    memberships: Mapped[list[UserProject]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UserProject(Base):
    __tablename__ = "user_projects"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # viewer | owner

    user: Mapped[User] = relationship(back_populates="memberships")
    project: Mapped[Project] = relationship()


# ── What the bot observed ─────────────────────────────────────────────────────


class BotCycle(Base):
    """One evaluation of one project. Doubles as the bot's heartbeat."""

    __tablename__ = "bot_cycles"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | ok | error
    checks_run: Mapped[int] = mapped_column(Integer, default=0)
    checks_failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Run(Base):
    """A scrape as the bot sees it: trigger row joined with its pipeline run."""

    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("project_id", "trigger_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    trigger_id: Mapped[str] = mapped_column(String(128))
    site: Mapped[str] = mapped_column(String(200), index=True)
    trigger_status: Mapped[str] = mapped_column(String(32))  # normalised: pending|running|succeeded|failed
    run_id: Mapped[str | None] = mapped_column(String(128), index=True)
    pipeline: Mapped[str | None] = mapped_column(String(200))
    run_status: Mapped[str | None] = mapped_column(String(32))  # ADF: Queued|InProgress|Succeeded|Failed|Cancelled
    requested_at: Mapped[datetime] = mapped_column(index=True)
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    duration_s: Mapped[float | None] = mapped_column(Float)
    rows_loaded: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    observed: Mapped[bool] = mapped_column(Boolean, default=False)  # output metrics recorded
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class MetricObservation(Base):
    """One metric for one completed run, the raw material for baselines and charts."""

    __tablename__ = "metric_observations"
    __table_args__ = (
        UniqueConstraint("project_id", "output_id", "site", "metric", "field", "run_ref"),
        Index("ix_metric_lookup", "project_id", "output_id", "site", "metric", "field", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    output_id: Mapped[str] = mapped_column(String(64))
    site: Mapped[str] = mapped_column(String(200))
    metric: Mapped[str] = mapped_column(String(32))  # row_count | null_rate | duplicate_rate | duration_s
    field: Mapped[str] = mapped_column(String(128), default="")
    run_ref: Mapped[str] = mapped_column(String(128))
    weekday: Mapped[int] = mapped_column(Integer)
    value: Mapped[float] = mapped_column(Float)
    observed_at: Mapped[datetime]


class Baseline(Base):
    """Learned normal range for a metric (cache of the latest computation, for display)."""

    __tablename__ = "baselines"
    __table_args__ = (UniqueConstraint("project_id", "output_id", "site", "metric", "field", "weekday"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    output_id: Mapped[str] = mapped_column(String(64))
    site: Mapped[str] = mapped_column(String(200))
    metric: Mapped[str] = mapped_column(String(32))
    field: Mapped[str] = mapped_column(String(128), default="")
    weekday: Mapped[int] = mapped_column(Integer)  # -1 = all days (fallback)
    basis: Mapped[str] = mapped_column(String(16))  # weekday | all_days
    samples: Mapped[int] = mapped_column(Integer)
    median: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SchemaSnapshot(Base):
    __tablename__ = "schema_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    output_id: Mapped[str] = mapped_column(String(64))
    columns: Mapped[list[Any]]
    fingerprint: Mapped[str] = mapped_column(String(64))
    captured_at: Mapped[datetime] = mapped_column(default=utcnow)


class CheckResult(Base):
    __tablename__ = "check_results"
    __table_args__ = (Index("ix_check_latest", "project_id", "check", "scope", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("bot_cycles.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    check: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(256))  # e.g. "site:Site A", "output:listings"
    status: Mapped[str] = mapped_column(String(8))  # pass | fail | skip
    severity: Mapped[str | None] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text, default="")
    actual: Mapped[float | None] = mapped_column(Float)
    expected_low: Mapped[float | None] = mapped_column(Float)
    expected_high: Mapped[float | None] = mapped_column(Float)
    run_ref: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


# ── Incidents and what happened to them ───────────────────────────────────────


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incident_open", "project_id", "fingerprint", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(256))
    scope: Mapped[str] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | acknowledged | resolved
    severity: Mapped[str] = mapped_column(String(16))
    checks: Mapped[list[Any]] = mapped_column(default=list)  # names of checks currently failing
    run_ref: Mapped[str | None] = mapped_column(String(128))

    summary: Mapped[str] = mapped_column(Text, default="")
    impact: Mapped[str] = mapped_column(Text, default="")
    likely_cause: Mapped[str] = mapped_column(Text, default="")
    suggested_action: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[str] = mapped_column(String(16), default="")  # low | medium | high
    analysis_source: Mapped[str] = mapped_column(String(16), default="rules")  # rules | ai
    evidence: Mapped[list[Any]] = mapped_column(default=list)

    opened_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    acknowledged_at: Mapped[datetime | None]
    acknowledged_by: Mapped[str | None] = mapped_column(String(200))
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[str | None] = mapped_column(String(200))  # a person's name, or "Alert Bot" when its checks pass again
    resolution: Mapped[str | None] = mapped_column(Text)  # how it was resolved, in words

    events: Mapped[list[IncidentEvent]] = relationship(
        back_populates="incident", order_by="IncidentEvent.at", cascade="all, delete-orphan"
    )


class IncidentEvent(Base):
    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    kind: Mapped[str] = mapped_column(String(24))  # opened|worsened|improved|resolved|acknowledged|notified|analysed|comment
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)

    incident: Mapped[Incident] = relationship(back_populates="events")


class AgentToolCall(Base):
    """Audit log: every tool the investigator (rules or AI) called for an incident."""

    __tablename__ = "agent_tool_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    agent: Mapped[str] = mapped_column(String(32))  # rules | investigator
    tool: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[Any] = mapped_column(JSON, default=None)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Notification(Base):
    """Outbox. Rows are written in the same transaction as the incident change,
    then dispatched; failures stay pending and are retried next cycle."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"), index=True)
    event: Mapped[str] = mapped_column(String(24))  # incident.opened|incident.worsened|incident.resolved|report.morning
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    payload: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|sent|dry_run|failed|muted
    channel: Mapped[str] = mapped_column(String(32), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    sent_at: Mapped[datetime | None]


class Mute(Base):
    __tablename__ = "mutes"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    scope: Mapped[str] = mapped_column(String(256), default="*")  # "*" or an incident scope
    reason: Mapped[str] = mapped_column(Text, default="")
    until: Mapped[datetime]
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class WebSession(Base):
    """A portal sign-in. Only a hash of the cookie value is stored, so a leaked database can't be used to log in."""

    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    mfa: Mapped[bool] = mapped_column(Boolean, default=False)
    azure_account: Mapped[str | None] = mapped_column(String(200))  # MSAL home_account_id (to connect the bot)


class IncidentEmbedding(Base):
    """An incident's meaning as a vector (AI embeddings), to find similar past incidents."""

    __tablename__ = "incident_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    vector: Mapped[list[Any]] = mapped_column(default=list)
    model: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BotState(Base):
    """Small pieces of the bot's own state that the portal shows, e.g. the data-quality connection status."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class AzureAccess(Base):
    """Who can see which Azure resource, as Azure itself answered for their own sign-in. Drives automatic project
    membership (and which sign-ins may read the resource for the bot)."""

    __tablename__ = "azure_access"
    __table_args__ = (UniqueConstraint("user_id", "resource_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    resource_id: Mapped[str] = mapped_column(String(400), index=True)  # lower-case ARM id (a resource group's for Logic Apps)
    project_key: Mapped[str] = mapped_column(String(100), index=True)
    home_account_id: Mapped[str] = mapped_column(String(200))
    seen_at: Mapped[datetime] = mapped_column(default=utcnow)


# ── Databases people connect for data-quality checks ──────────────────────────


class DbConnection(Base):
    """A database one person connected (SQL Server or Snowflake). It stays connected: the bot reuses it on every check
    and the next time they sign in. The password / key is stored encrypted (never returned, never logged)."""

    __tablename__ = "db_connections"
    __table_args__ = (UniqueConstraint("user_id", "kind", "host", "database", "username"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16))           # sqlserver | snowflake (sqlite: tests)
    host: Mapped[str] = mapped_column(String(255))          # server / Snowflake account identifier
    port: Mapped[int | None] = mapped_column(Integer)
    database: Mapped[str] = mapped_column(String(128))
    auth: Mapped[str] = mapped_column(String(16))           # sql | entra | password | keypair
    username: Mapped[str] = mapped_column(String(200), default="")
    secret_enc: Mapped[str] = mapped_column(Text, default="")  # encrypted password / private key
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)  # non-secret: encrypt, trust_cert, warehouse, role
    home_account_id: Mapped[str | None] = mapped_column(String(200))  # entra: whose Microsoft sign-in
    status: Mapped[str] = mapped_column(String(16), default="new")  # ok | error | new
    can_write: Mapped[bool | None] = mapped_column(Boolean)  # the login could change data (the bot never does)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_ok_at: Mapped[datetime | None] = mapped_column()
    last_used_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class DqMapping(Base):
    """Which pipeline loads which table, as a person chose it: the table's data is checked after that pipeline loads.
    Mapping a table is what approves it for checking (the allowlist)."""

    __tablename__ = "dq_mappings"
    __table_args__ = (UniqueConstraint("project_id", "connection_id", "schema_name", "table_name", "pipeline"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("db_connections.id", ondelete="CASCADE"), index=True)
    pipeline: Mapped[str] = mapped_column(String(200))
    schema_name: Mapped[str] = mapped_column(String(128))  # exactly as the database stores it
    table_name: Mapped[str] = mapped_column(String(128))
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)  # key, date column, sensitive, rules, ranges …
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Azure decides: when the pipeline is renamed or deleted, or no longer loads this table, the mapping is retired (kept,
    # not checked) and comes back by itself if the pipeline loads the table again. False: a person said "keep checking".
    follow_azure: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    retired_at: Mapped[datetime | None]
    retired_reason: Mapped[str | None] = mapped_column(Text)
    retired_by: Mapped[str | None] = mapped_column(String(16))  # azure: the pipeline changed · database: the table is gone
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)

    connection: Mapped[DbConnection] = relationship()


class FixAttempt(Base):
    """One set of fix steps shown for a failure, and whether it worked. The answers are how the bot learns which fix
    works for which error (and what the team did when its own steps weren't enough)."""

    __tablename__ = "fix_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(160), index=True)  # incident:<id> | run:<run id>
    incident_id: Mapped[int | None] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(String(128))
    pipeline: Mapped[str | None] = mapped_column(String(200))
    signature: Mapped[str] = mapped_column(String(200), index=True)  # the error, as the library knows it
    variant: Mapped[str] = mapped_column(String(80))  # which approach: a library key, ai:<n>, team:<attempt id>, escalate
    source: Mapped[str] = mapped_column(String(16))  # library | ai | team | escalate
    title: Mapped[str] = mapped_column(String(200))
    steps: Mapped[list[Any]] = mapped_column(default=list)
    verify: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | worked | failed
    reason: Mapped[str | None] = mapped_column(Text)  # why it didn't work, in their words
    note: Mapped[str | None] = mapped_column(Text)  # what they actually did, when it worked
    parent_id: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    answered_by: Mapped[str | None] = mapped_column(String(200))
    answered_at: Mapped[datetime | None]


class AiCall(Base):
    """One request to an AI model, metered: the real token counts the provider reported (or an estimate, flagged), how
    long it took, which part of the app asked, and what it actually cost. Never the prompt or the answer."""

    __tablename__ = "ai_calls"
    __table_args__ = (Index("ix_ai_calls_at", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow)
    feature: Mapped[str] = mapped_column(String(32))  # incident_analysis | triage | similar | ask | charts | dq | report | voice | other
    kind: Mapped[str] = mapped_column(String(16))  # chat | decide | embed | speech
    model: Mapped[str] = mapped_column(String(200))  # the model asked for
    served_model: Mapped[str | None] = mapped_column(String(200))  # the model that answered (as the provider names it)
    status: Mapped[str] = mapped_column(String(16))  # ok | empty | error | retry
    http_status: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)  # the provider didn't report usage: counted from text length
    cost_usd: Mapped[float | None] = mapped_column(Float)  # what the provider charged (free models: 0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    characters: Mapped[int] = mapped_column(Integer, default=0)  # speech: text read aloud
    audio_seconds: Mapped[float | None] = mapped_column(Float)  # speech: length of the audio returned
    project_key: Mapped[str | None] = mapped_column(String(100), index=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(200))


class UserChart(Base):
    """A chart someone asked the bot for and pinned to a project's overview. Only its owner sees it. The spec names a
    metric from the bot's own catalogue (never a query); the numbers are recomputed every time it's shown."""

    __tablename__ = "user_charts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_key: Mapped[str] = mapped_column(String(100), index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(default=dict)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BaselineReset(Base):
    """Someone said "this is the new normal" (from an incident): from `since`, only newer measurements teach the
    baseline. Empty output / site / field = every one of them for that metric."""

    __tablename__ = "baseline_resets"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    output_id: Mapped[str | None] = mapped_column(String(300))
    site: Mapped[str | None] = mapped_column(String(200))
    metric: Mapped[str] = mapped_column(String(64))
    field: Mapped[str | None] = mapped_column(String(128))
    since: Mapped[datetime]
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"))
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
