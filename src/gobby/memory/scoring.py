"""Scoring helpers for memory search ranking."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from gobby.utils.datetime import parse_stored_datetime


def recency_anchor(
    updated_at: datetime | str | None,
    last_accessed_at: datetime | str | None,
) -> datetime | None:
    """Return the later of a memory's last update and its last direct fetch.

    A fetch is evidence the memory is still in use, so decay counts from
    whichever happened last. Missing or unparseable timestamps are ignored;
    ``None`` means neither is known.
    """
    anchors: list[datetime] = []
    for value in (updated_at, last_accessed_at):
        try:
            parsed = parse_stored_datetime(value)
        except ValueError:
            continue
        if parsed is not None:
            anchors.append(parsed)
    return max(anchors, default=None)


def temporal_decay(
    updated_at: datetime | str | None,
    half_life_days: float,
    now: datetime | None = None,
) -> float:
    """Return a multiplicative decay factor in (0, 1] based on memory age.

    Uses a half-life model: ``factor = 0.5 ^ (age_days / half_life_days)``.

    Args:
        updated_at: Timestamp of last memory update.
        half_life_days: Number of days after which the factor reaches 0.5.
            Set to 0 (or negative) to disable decay (returns 1.0).
        now: Reference time for age calculation. Defaults to ``datetime.now(UTC)``.

    Returns:
        Decay factor between 0 (exclusive) and 1 (inclusive).
        Returns 1.0 on parse failure or when decay is disabled.
    """
    if half_life_days <= 0:
        return 1.0
    try:
        updated = parse_stored_datetime(updated_at)
        if updated is None:
            return 1.0
        if now is None:
            now = datetime.now(UTC)
        age_days = max((now - updated).total_seconds() / 86400.0, 0.0)
        return math.pow(0.5, age_days / half_life_days)
    except (ValueError, TypeError):
        return 1.0
