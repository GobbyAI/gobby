"""SQLite source schema validation for one-shot PostgreSQL imports."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from gobby.storage.migrations import BASELINE_VERSION

_EXPECTED_SQLITE_SCHEMA_FINGERPRINTS = (
    # Legacy v260 source with schema_migrations bookkeeping.
    "fbe2914e66ab5d608c8ffc38638a761874fb5a19ff6e76f8bae9447871002269",
    # Legacy v260 source with schema_version bookkeeping.
    "2868b075dfd8258def472aa2912d8231e02b198834406a5be73961fc74fd4933",
)


@dataclass(frozen=True)
class SqliteSchemaValidation:
    ok: bool
    message: str
    version: int | None
    fingerprint: str | None
    expected_fingerprints: tuple[str, ...]


def validate_sqlite_source_schema(source: sqlite3.Connection) -> SqliteSchemaValidation:
    """Validate source schema version and DDL fingerprint against the supported baseline."""
    version = sqlite_schema_version(source)
    expected = expected_sqlite_schema_fingerprints()
    fingerprint = sqlite_schema_fingerprint(source)
    if version is None:
        return SqliteSchemaValidation(
            False, "SQLite schema baseline missing", version, fingerprint, expected
        )
    if version != BASELINE_VERSION:
        return SqliteSchemaValidation(
            False,
            f"SQLite schema baseline mismatch: expected {BASELINE_VERSION}, found {version}",
            version,
            fingerprint,
            expected,
        )
    if fingerprint not in expected:
        return SqliteSchemaValidation(
            False,
            "SQLite schema fingerprint mismatch: source schema drifted from supported baseline",
            version,
            fingerprint,
            expected,
        )
    return SqliteSchemaValidation(
        True, f"SQLite schema baseline v{version} fingerprint ok", version, fingerprint, expected
    )


def sqlite_schema_version(source: sqlite3.Connection) -> int | None:
    for table in ("schema_migrations", "schema_version"):
        try:
            row = source.execute(
                f"SELECT MAX(version) AS version FROM {_quote_identifier(table)}"
            ).fetchone()
        except sqlite3.Error:
            continue
        if row is None:
            continue
        version = _row_value(row, "version")
        return int(version) if version is not None else None
    return None


def sqlite_schema_fingerprint(source: sqlite3.Connection) -> str:
    payload = json.dumps(_sqlite_schema_objects(source), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expected_sqlite_schema_fingerprints() -> tuple[str, ...]:
    return _EXPECTED_SQLITE_SCHEMA_FINGERPRINTS


def _sqlite_schema_objects(source: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    rows = source.execute(
        """
        SELECT type, name, tbl_name, sql
          FROM sqlite_master
         WHERE sql IS NOT NULL
           AND name NOT LIKE 'sqlite_%'
         ORDER BY type, name, tbl_name, sql
        """
    ).fetchall()
    return [
        (
            str(_row_value(row, "type", 0)),
            str(_row_value(row, "name", 1)),
            str(_row_value(row, "tbl_name", 2)),
            _normalize_sql(str(_row_value(row, "sql", 3))),
        )
        for row in rows
    ]


def _normalize_sql(value: str) -> str:
    return " ".join(value.split())


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
