from __future__ import annotations

import importlib
import inspect
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.unit


def _postgres_module():
    return importlib.import_module("gobby.storage.hub.postgres")


def test_postgres_hub_database_exposes_backend_neutral_surface() -> None:
    module = _postgres_module()

    assert module.PostgresHubDatabase.dialect == "postgres"

    transaction = inspect.signature(module.PostgresHubDatabase.transaction)
    assert list(transaction.parameters) == ["self"]

    transaction_immediate = inspect.signature(module.PostgresHubDatabase.transaction_immediate)
    assert list(transaction_immediate.parameters) == ["self", "lock"]

    for method in (
        "execute",
        "executemany",
        "fetchone",
        "fetchall",
        "safe_update",
        "apply_migrations",
        "close",
    ):
        assert hasattr(module.PostgresHubDatabase, method), method


class _FakePostgresConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, object]] = []
        self.executemany_calls: list[tuple[str, list[tuple[object, ...]]]] = []

    def execute(self, sql: str, params: object = ()):
        self.execute_calls.append((sql, params))
        return object()

    def executemany(self, sql: str, rows) -> None:
        self.executemany_calls.append((sql, [tuple(row) for row in rows]))


def test_postgres_transaction_execute_passes_sql_and_params_directly() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute("SELECT %s, %s, 'literal%%'", ("one", "two"))

    assert conn.execute_calls == [("SELECT %s, %s, 'literal%%'", ("one", "two"))]


def test_postgres_transaction_execute_preserves_mapping_params() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)
    params = {"value": 1, "title": "demo"}

    tx.execute("SELECT %(value)s WHERE title = %(title)s", params)

    assert conn.execute_calls == [("SELECT %(value)s WHERE title = %(title)s", params)]


def test_postgres_transaction_executemany_passes_rows_directly() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.executemany(
        "INSERT INTO direct_rows (a, b) VALUES (%s, %s)",
        [("left", "right"), ("first", "second")],
    )

    assert conn.executemany_calls == [
        (
            "INSERT INTO direct_rows (a, b) VALUES (%s, %s)",
            [("left", "right"), ("first", "second")],
        )
    ]


def test_postgres_transaction_executemany_empty_rows_skips_driver() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.executemany("INSERT INTO direct_rows (a) VALUES (%s)", [])

    assert conn.executemany_calls == []


def test_postgres_safe_update_builds_psycopg_where_style() -> None:
    module = _postgres_module()

    assert module._build_safe_update(
        "sessions",
        {"message_count": 3, "turn_count": 2},
        "id = %s",
        ("session-1",),
    ) == (
        "UPDATE sessions SET message_count = %s, turn_count = %s WHERE id = %s",
        (3, 2, "session-1"),
    )


def test_postgres_safe_update_rejects_invalid_identifiers() -> None:
    module = _postgres_module()

    with pytest.raises(ValueError, match="invalid SQL identifier"):
        module._build_safe_update(
            "sessions; DROP TABLE sessions", {"status": "paused"}, "id = %s", ()
        )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def test_postgres_cursor_normalizes_jsonb_values_to_json_text() -> None:
    module = _postgres_module()
    cursor = module._PostgresCursor(
        _FakeResult(
            [
                {
                    "name": "profile",
                    "skip_stages_json": ["merge", "qa"],
                    "metadata_json": {"b": 2, "a": 1},
                }
            ]
        )
    )

    assert cursor.fetchone() == {
        "name": "profile",
        "skip_stages_json": '["merge","qa"]',
        "metadata_json": '{"a":1,"b":2}',
    }


def test_postgres_cursor_normalizes_datetime_values_to_iso_text() -> None:
    module = _postgres_module()
    cursor = module._PostgresCursor(
        _FakeResult(
            [
                {
                    "id": "cron",
                    "last_run_at": datetime(2026, 5, 21, 5, 30, tzinfo=UTC),
                }
            ]
        )
    )

    assert cursor.fetchone() == {
        "id": "cron",
        "last_run_at": "2026-05-21T05:30:00+00:00",
    }
