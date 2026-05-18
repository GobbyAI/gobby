"""Shared helper functions for agent run storage."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _positive_rowcount(cursor: Any) -> int:
    """Return cursor.rowcount when it is a positive int, otherwise zero."""
    rowcount = getattr(cursor, "rowcount", 0)
    return rowcount if isinstance(rowcount, int) and rowcount > 0 else 0


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO format."""
    return datetime.now(UTC).isoformat()
