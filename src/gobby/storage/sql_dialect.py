"""Small SQL expression helpers for PostgreSQL storage."""

from __future__ import annotations

import re
from typing import Literal

StorageDialect = Literal["sqlite", "postgres"]
IntervalUnit = Literal["second", "minute", "hour", "day"]

_JSON_PATH_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def dialect_of(db: object) -> StorageDialect:
    """Return the runtime storage dialect."""
    return "postgres"


def is_postgres(db: object) -> bool:
    return True


def json_text_expr(db: object, column: str, *path: str) -> str:
    """Return a scalar JSON text extraction expression."""
    if not path:
        raise ValueError("JSON path must contain at least one key")
    for key in path:
        if not _JSON_PATH_KEY_RE.fullmatch(key):
            raise ValueError(f"unsupported JSON path key: {key!r}")

    return f"{column} #>> '{{{','.join(path)}}}'"


def older_than_now_expr(db: object, column: str, param: str, unit: IntervalUnit) -> str:
    return f"{column} < NOW() - ({param} * INTERVAL '1 {unit}')"


def newer_than_now_expr(db: object, column: str, param: str, unit: IntervalUnit) -> str:
    return f"{column} >= NOW() - ({param} * INTERVAL '1 {unit}')"


def timestamp_plus_seconds_before_now_expr(
    db: object,
    timestamp_column: str,
    seconds_column: str,
) -> str:
    return f"{timestamp_column} + ({seconds_column} * INTERVAL '1 second') < NOW()"


def elapsed_seconds_greater_than_expr(
    db: object,
    timestamp_column: str,
    seconds_column: str,
) -> str:
    return f"EXTRACT(EPOCH FROM (NOW() - {timestamp_column})) > {seconds_column}"
