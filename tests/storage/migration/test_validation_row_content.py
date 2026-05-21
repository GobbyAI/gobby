from __future__ import annotations

import importlib
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.storage.migrations import BASELINE_VERSION

pytestmark = pytest.mark.unit


class _Row(dict[str, Any]):
    def __init__(self, *values: Any, **items: Any) -> None:
        super().__init__(items)
        self._values = values

    def __getitem__(self, key: object) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _PostgresRows:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.tables = tables

    def execute(self, sql: object, params: object = ()) -> _Result:
        text = str(sql).lower()
        if "pg_tables" in text or "information_schema.tables" in text:
            return _Result(
                [_Row(name, tablename=name, table_name=name) for name in sorted(self.tables)]
            )
        if "count" in text:
            table = _table_from_select(text)
            count = len(self.tables[table])
            return _Result([_Row(count, count=count, row_count=count)])
        if text.lstrip().startswith("select"):
            table = _table_from_select(text)
            selected = _selected_columns(text)
            if selected is None:
                return _Result([_Row(**row) for row in self.tables[table]])
            return _Result(
                [_Row(**{column: row[column] for column in selected}) for row in self.tables[table]]
            )
        raise AssertionError(f"unexpected SQL: {sql}")


def test_validate_migration_detects_row_count_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    validation = importlib.import_module("gobby.storage.migration.validation")
    _allow_minimal_source_schema(monkeypatch, validation)
    source = _sqlite_source()
    target = _PostgresRows(
        {
            "tasks": [
                {"id": 1, "title": "imported", "created_at": "2026-01-01T00:00:00Z"},
                {"id": 2, "title": "extra", "created_at": "2026-01-02T00:00:00Z"},
            ]
        }
    )

    with pytest.raises(validation.MigrationValidationError, match="tasks.*row count"):
        validation.validate_migration(source, target)


def test_validate_migration_detects_content_mismatch_with_equal_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation = importlib.import_module("gobby.storage.migration.validation")
    _allow_minimal_source_schema(monkeypatch, validation)
    source = _sqlite_source()
    target = _PostgresRows(
        {"tasks": [{"id": 1, "title": "different", "created_at": "2026-01-01T00:00:00Z"}]}
    )

    with pytest.raises(validation.MigrationValidationError, match="tasks.*content"):
        validation.validate_migration(source, target)


def test_validate_migration_ignores_postgres_generated_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation = importlib.import_module("gobby.storage.migration.validation")
    _allow_minimal_source_schema(monkeypatch, validation)
    source = _sqlite_source()
    target = _PostgresRows(
        {
            "tasks": [
                {
                    "id": 1,
                    "title": "imported",
                    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "generated_search_text": "imported",
                }
            ]
        }
    )

    validation.validate_migration(source, target)


def test_validate_migration_rejects_source_without_schema_bookkeeping() -> None:
    validation = importlib.import_module("gobby.storage.migration.validation")
    source = _sqlite_source(with_schema_version=False)
    target = _PostgresRows(
        {"tasks": [{"id": 1, "title": "imported", "created_at": "2026-01-01T00:00:00Z"}]}
    )

    with pytest.raises(validation.MigrationValidationError, match="schema"):
        validation.validate_migration(source, target)


@pytest.mark.parametrize("schema_version", [BASELINE_VERSION - 1, BASELINE_VERSION + 1])
def test_validate_migration_rejects_source_with_unsupported_schema_version(
    schema_version: int,
) -> None:
    validation = importlib.import_module("gobby.storage.migration.validation")
    source = _sqlite_source(schema_version=schema_version)
    target = _PostgresRows(
        {"tasks": [{"id": 1, "title": "imported", "created_at": "2026-01-01T00:00:00Z"}]}
    )

    with pytest.raises(validation.MigrationValidationError, match="baseline mismatch"):
        validation.validate_migration(source, target)


def test_validate_migration_rejects_source_with_schema_fingerprint_drift() -> None:
    validation = importlib.import_module("gobby.storage.migration.validation")
    source = _sqlite_source()
    target = _PostgresRows(
        {"tasks": [{"id": 1, "title": "imported", "created_at": "2026-01-01T00:00:00Z"}]}
    )

    with pytest.raises(validation.MigrationValidationError, match="fingerprint"):
        validation.validate_migration(source, target)


def _allow_minimal_source_schema(monkeypatch: pytest.MonkeyPatch, validation: Any) -> None:
    monkeypatch.setattr(
        validation,
        "validate_sqlite_source_schema",
        lambda _source: SimpleNamespace(ok=True, message="SQLite schema baseline test bypass"),
    )


def _sqlite_source(
    *,
    with_schema_version: bool = True,
    schema_version: int = BASELINE_VERSION,
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if with_schema_version:
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (schema_version,))
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, created_at) VALUES (1, 'imported', '2026-01-01 00:00:00')"
    )
    return conn


def _table_from_select(sql: str) -> str:
    marker = " from "
    start = sql.index(marker) + len(marker)
    token = sql[start:].split()[0]
    return token.strip('"')


def _selected_columns(sql: str) -> list[str] | None:
    selected = sql.split(" from ", 1)[0].removeprefix("select").strip()
    if selected == "*":
        return None
    return [column.strip().strip('"') for column in selected.split(",")]
