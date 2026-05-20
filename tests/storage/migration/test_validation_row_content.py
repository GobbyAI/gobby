from __future__ import annotations

import importlib
import sqlite3
from typing import Any

import pytest

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


def test_validate_migration_detects_row_count_mismatch() -> None:
    validation = importlib.import_module("gobby.storage.migration.validation")
    source = _sqlite_source()
    target = _PostgresRows({"tasks": [{"id": 1, "title": "imported"}, {"id": 2, "title": "extra"}]})

    with pytest.raises(validation.MigrationValidationError, match="tasks.*row count"):
        validation.validate_migration(source, target)


def test_validate_migration_detects_content_mismatch_with_equal_counts() -> None:
    validation = importlib.import_module("gobby.storage.migration.validation")
    source = _sqlite_source()
    target = _PostgresRows({"tasks": [{"id": 1, "title": "different"}]})

    with pytest.raises(validation.MigrationValidationError, match="tasks.*content"):
        validation.validate_migration(source, target)


def test_validate_migration_ignores_postgres_generated_columns() -> None:
    validation = importlib.import_module("gobby.storage.migration.validation")
    source = _sqlite_source()
    target = _PostgresRows(
        {"tasks": [{"id": 1, "title": "imported", "generated_search_text": "imported"}]}
    )

    validation.validate_migration(source, target)


def _sqlite_source() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    conn.execute("INSERT INTO tasks (id, title) VALUES (1, 'imported')")
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
