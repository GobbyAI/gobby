"""Datetime parsing helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def parse_stored_datetime(value: datetime | str | None) -> datetime | None:
    """Parse a stored ISO timestamp and normalize it to UTC.

    Legacy rows may contain naive ISO strings. Treat those as UTC so arithmetic
    against aware ``datetime.now(UTC)`` stays valid.
    """
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
