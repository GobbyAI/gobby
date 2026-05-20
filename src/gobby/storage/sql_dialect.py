"""Small SQL expression helpers for storage dialect branches."""

from __future__ import annotations

import re
from typing import Literal, cast

StorageDialect = Literal["sqlite", "postgres"]
IntervalUnit = Literal["second", "minute", "hour", "day"]

_JSON_PATH_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def dialect_of(db: object) -> StorageDialect:
    """Return the storage dialect, defaulting legacy database objects to SQLite."""
    dialect = getattr(db, "dialect", "sqlite")
    if dialect in {"sqlite", "postgres"}:
        return cast(StorageDialect, dialect)
    raise ValueError(f"unsupported storage dialect: {dialect!r}")


def is_postgres(db: object) -> bool:
    return dialect_of(db) == "postgres"


def json_text_expr(db: object, column: str, *path: str) -> str:
    """Return a scalar JSON text extraction expression."""
    if not path:
        raise ValueError("JSON path must contain at least one key")
    for key in path:
        if not _JSON_PATH_KEY_RE.fullmatch(key):
            raise ValueError(f"unsupported JSON path key: {key!r}")

    if is_postgres(db):
        return f"{column} #>> '{{{','.join(path)}}}'"
    return f"json_extract({column}, '$.{'.'.join(path)}')"


def older_than_now_expr(db: object, column: str, param: str, unit: IntervalUnit) -> str:
    if is_postgres(db):
        return f"{column} < NOW() - ({param} * INTERVAL '1 {unit}')"
    return f"datetime({column}) < datetime('now', 'utc', '-' || {param} || ' {_plural(unit)}')"


def newer_than_now_expr(db: object, column: str, param: str, unit: IntervalUnit) -> str:
    if is_postgres(db):
        return f"{column} >= NOW() - ({param} * INTERVAL '1 {unit}')"
    return f"datetime({column}) >= datetime('now', 'utc', '-' || {param} || ' {_plural(unit)}')"


def timestamp_plus_seconds_before_now_expr(
    db: object,
    timestamp_column: str,
    seconds_column: str,
) -> str:
    if is_postgres(db):
        return f"{timestamp_column} + ({seconds_column} * INTERVAL '1 second') < NOW()"
    return f"datetime({timestamp_column}, '+' || {seconds_column} || ' seconds') < datetime('now')"


def elapsed_seconds_greater_than_expr(
    db: object,
    timestamp_column: str,
    seconds_column: str,
) -> str:
    if is_postgres(db):
        return f"EXTRACT(EPOCH FROM (NOW() - {timestamp_column})) > {seconds_column}"
    return f"(julianday('now') - julianday({timestamp_column})) * 86400 > {seconds_column}"


def _plural(unit: IntervalUnit) -> str:
    return f"{unit}s"
