"""Small SQL expression helpers for PostgreSQL storage."""

from __future__ import annotations

import re
from typing import Any, Literal, cast

StorageDialect = Literal["sqlite", "postgres"]
IntervalUnit = Literal["second", "minute", "hour", "day"]

_JSON_PATH_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def dialect_of(db: object) -> StorageDialect:
    """Return the runtime storage dialect."""
    dialect = getattr(db, "dialect", "postgres")
    if dialect in ("sqlite", "postgres"):
        return cast(StorageDialect, dialect)
    return "postgres"


def is_postgres(db: object) -> bool:
    return dialect_of(db) == "postgres"


def table_column_names(db: Any, table_name: str) -> set[str]:
    """Return column names for a table using the current storage dialect."""
    if not _SQL_IDENTIFIER_RE.fullmatch(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    if is_postgres(db):
        rows = db.fetchall(
            """
            SELECT column_name AS name
              FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = ?
            """,
            (table_name,),
        )
    else:
        rows = db.fetchall(f"PRAGMA table_info({table_name})")  # nosec B608
    return {str(row["name"]) for row in rows}


def json_text_expr(db: object, column: str, *path: str) -> str:
    """Return a scalar JSON text extraction expression."""
    if not path:
        raise ValueError("JSON path must contain at least one key")
    for key in path:
        if not _JSON_PATH_KEY_RE.fullmatch(key):
            raise ValueError(f"unsupported JSON path key: {key!r}")

    if not is_postgres(db):
        return f"json_extract({column}, '$.{'.'.join(path)}')"
    return f"{column} #>> '{{{','.join(path)}}}'"


def older_than_now_expr(db: object, column: str, param: str, unit: IntervalUnit) -> str:
    if not is_postgres(db):
        return f"{column} < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-' || {param} || ' {unit}s')"
    return f"{column} < NOW() - ({param}::double precision * INTERVAL '1 {unit}')"


def newer_than_now_expr(db: object, column: str, param: str, unit: IntervalUnit) -> str:
    if not is_postgres(db):
        return f"{column} >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-' || {param} || ' {unit}s')"
    return f"{column} >= NOW() - ({param}::double precision * INTERVAL '1 {unit}')"


def timestamp_plus_seconds_before_now_expr(
    db: object,
    timestamp_column: str,
    seconds_column: str,
) -> str:
    if not is_postgres(db):
        return (
            f"datetime({timestamp_column}, '+' || {seconds_column} || ' seconds') < datetime('now')"
        )
    return f"{timestamp_column} + ({seconds_column} * INTERVAL '1 second') < NOW()"


def elapsed_seconds_greater_than_expr(
    db: object,
    timestamp_column: str,
    seconds_column: str,
) -> str:
    if not is_postgres(db):
        return f"(julianday('now') - julianday({timestamp_column})) * 86400 > {seconds_column}"
    return f"EXTRACT(EPOCH FROM (NOW() - {timestamp_column})) > {seconds_column}"
