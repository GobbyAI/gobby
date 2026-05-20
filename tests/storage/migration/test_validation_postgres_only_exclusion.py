from __future__ import annotations

import importlib
import sqlite3
from typing import Any

import pytest

from gobby.storage.hub.postgres import _PRE_BASELINE_INFRA_TABLES

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


class _PostgresFixture:
    def __init__(
        self,
        *,
        application_tables: dict[str, list[dict[str, Any]]],
        postgres_only_tables: set[str],
    ) -> None:
        self.application_tables = application_tables
        self.postgres_only_tables = postgres_only_tables

    def execute(self, sql: object, params: object = ()) -> _Result:
        text = str(sql).lower()
        if "pg_tables" in text or "information_schema.tables" in text:
            rows = [
                _Row(name, tablename=name, table_name=name)
                for name in sorted(set(self.application_tables) | self.postgres_only_tables)
            ]
            return _Result(rows)
        if "count" in text:
            table = _table_from_select(text)
            count = len(self.application_tables[table])
            return _Result(
                [
                    _Row(
                        count,
                        count=count,
                        row_count=count,
                    )
                ]
            )
        if text.lstrip().startswith("select"):
            table = _table_from_select(text)
            return _Result([_Row(**row) for row in self.application_tables[table]])
        raise AssertionError(f"unexpected SQL: {sql}")


def test_pgaudit_probe_excluded_unknown_table_fails() -> None:
    validation = importlib.import_module("gobby.storage.migration.validation")
    source = _sqlite_source()
    target = _PostgresFixture(
        application_tables={"tasks": [{"id": 1, "title": "imported"}]},
        postgres_only_tables={
            *_PRE_BASELINE_INFRA_TABLES,
            "gobby_migration_state",
            "schema_migrations",
        },
    )

    assert validation._POSTGRES_ONLY_TABLES == _PRE_BASELINE_INFRA_TABLES | {
        "gobby_migration_state",
        "schema_migrations",
    }
    validation.validate_migration(source, target)

    unexpected = _PostgresFixture(
        application_tables={"tasks": [{"id": 1, "title": "imported"}]},
        postgres_only_tables={"_pgaudit_probe", "schema_migrations", "surprise_table"},
    )

    with pytest.raises(validation.MigrationValidationError, match="surprise_table"):
        validation.validate_migration(source, unexpected)


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
