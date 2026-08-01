"""Shared HTTP retry header parsing."""

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def parse_retry_after(
    value: str | None,
    *,
    max_delay: float,
    now: datetime | None = None,
) -> float | None:
    """Parse a Retry-After value and clamp it to a non-negative delay."""
    if not value:
        return None

    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current_time = now if now is not None else datetime.now(UTC)
        delay = (retry_at - current_time).total_seconds()

    if not math.isfinite(delay):
        return max_delay
    return min(max(0.0, delay), max_delay)
