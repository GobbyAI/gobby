"""SQLite source schema validation for one-shot PostgreSQL imports."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from functools import cache
from typing import Any

from gobby.storage.migration.tables import sqlite_application_tables
from gobby.storage.migrations import BASELINE_VERSION, _sqlite_baseline_sql


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
    if fingerprint not in expected and not _migration_table_set_matches_baseline(source):
        return SqliteSchemaValidation(
            False,
            "SQLite schema fingerprint mismatch: source schema drifted from supported baseline",
            version,
            fingerprint,
            expected,
        )
    if fingerprint not in expected:
        return SqliteSchemaValidation(
            True,
            (
                f"SQLite schema baseline v{version} table set ok; raw DDL fingerprint differs "
                "from flattened baseline"
            ),
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


@cache
def expected_sqlite_schema_fingerprints() -> tuple[str, ...]:
    return tuple(_baseline_fingerprint(table) for table in ("schema_migrations", "schema_version"))


def _baseline_fingerprint(version_table: str) -> str:
    conn = _baseline_connection(version_table)
    try:
        return sqlite_schema_fingerprint(conn)
    finally:
        conn.close()


def _migration_table_set_matches_baseline(source: sqlite3.Connection) -> bool:
    source_tables = sqlite_application_tables(source)
    return source_tables in expected_sqlite_migration_table_sets()


@cache
def expected_sqlite_migration_table_sets() -> frozenset[frozenset[str]]:
    return frozenset(
        _baseline_table_set(table) for table in ("schema_migrations", "schema_version")
    )


def _baseline_table_set(version_table: str) -> frozenset[str]:
    conn = _baseline_connection(version_table)
    try:
        return frozenset(sqlite_application_tables(conn))
    finally:
        conn.close()


def _baseline_connection(version_table: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_sqlite_baseline_sql(version_table))
    return conn


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
