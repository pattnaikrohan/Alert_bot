"""Time helpers. Convention: every timestamp stored or compared is naive UTC."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def local_weekday(value: datetime, tz: ZoneInfo) -> int:
    """Weekday (Mon=0) of a naive-UTC timestamp, in the configured business timezone."""
    return value.replace(tzinfo=UTC).astimezone(tz).weekday()


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.replace(microsecond=0).isoformat() + "Z"


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
