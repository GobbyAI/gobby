"""Small SQL expression helpers for PostgreSQL storage."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, cast

StorageDialect = Literal["postgres"]
IntervalUnit = Literal["second", "minute", "hour", "day"]

_JSON_PATH_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def dialect_of(db: object) -> StorageDialect:
    """Return the runtime storage dialect."""
    dialect = getattr(db, "dialect", "postgres")
    if dialect == "postgres":
        return cast(StorageDialect, dialect)
    return "postgres"


def is_postgres(db: object) -> bool:
    return True


def table_column_names(db: Any, table_name: str) -> set[str]:
    """Return column names for a table in the current PostgreSQL schema."""
    if not _SQL_IDENTIFIER_RE.fullmatch(table_name):
        raise ValueError(f"Invalid table name: {table_name!r}")
    rows = db.fetchall(
        """
        SELECT column_name AS name
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = ?
        """,
        (table_name,),
    )
    return {str(row["name"]) for row in rows}


def json_text_expr(db: object, column: str, *path: str) -> str:
    """Return a scalar JSON text extraction expression."""
    if not path:
        raise ValueError("JSON path must contain at least one key")
    for key in path:
        if not _JSON_PATH_KEY_RE.fullmatch(key):
            raise ValueError(f"unsupported JSON path key: {key!r}")

    return f"{column} #>> '{{{','.join(path)}}}'"


def json_array_contains_condition(
    db: object, column: str, value: str
) -> tuple[str, tuple[str, ...]]:
    """Return a SQL condition and params for exact JSON string-array membership."""
    return f"{column} @> ?::jsonb", (json.dumps([value]),)


def older_than_now_expr(db: object, column: str, param: str, unit: IntervalUnit) -> str:
    return f"{column} < NOW() - ({param}::double precision * INTERVAL '1 {unit}')"


def newer_than_now_expr(db: object, column: str, param: str, unit: IntervalUnit) -> str:
    return f"{column} >= NOW() - ({param}::double precision * INTERVAL '1 {unit}')"


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
