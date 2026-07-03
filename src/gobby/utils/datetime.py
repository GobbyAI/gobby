"""Datetime helpers for storage and API boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any


def utc_now() -> datetime:
    """Return the current instant as a UTC-aware datetime."""
    return datetime.now(UTC)


def to_aware_utc(value: datetime) -> datetime:
    """Normalize a datetime to UTC, treating naive values as legacy UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_stored_datetime(value: datetime | str | None) -> datetime | None:
    """Parse a stored ISO timestamp and normalize it to UTC.

    Legacy rows may contain naive ISO strings. Treat those as UTC so arithmetic
    against aware ``datetime.now(UTC)`` stays valid.
    """
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return to_aware_utc(parsed)


def datetime_to_iso(value: datetime | None) -> str | None:
    """Serialize a datetime for an external JSON/text boundary."""
    if value is None:
        return None
    return to_aware_utc(value).isoformat()


def datetime_to_required_iso(value: datetime) -> str:
    """Serialize a required datetime for an external JSON/text boundary."""
    return to_aware_utc(value).isoformat()


def to_json_safe(value: Any) -> Any:
    """Recursively serialize datetime/date values for JSON boundaries."""
    if isinstance(value, datetime):
        return datetime_to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [to_json_safe(item) for item in value]
    return value
