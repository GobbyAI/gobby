"""Value normalization helpers for hub database migration."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_EPOCH_RE = re.compile(r"^[+-]?\d{9,13}$")
_TIMESTAMP_COLUMN_NAMES = {"timestamp", "event_at", "sent_at", "read_at"}


def normalize_timestamp_like_value(column: str, value: Any) -> Any:
    if value is None or not _is_timestamp_like_column(column):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not _EPOCH_RE.match(stripped):
            return value
        seconds = _epoch_seconds(int(stripped))
        return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")
    if isinstance(value, int):
        seconds = _epoch_seconds(value)
        return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")
    return value


def _is_timestamp_like_column(column: str) -> bool:
    return column.endswith("_at") or column in _TIMESTAMP_COLUMN_NAMES


def _epoch_seconds(value: int) -> float:
    if abs(value) >= 10_000_000_000:
        return value / 1000
    return float(value)
