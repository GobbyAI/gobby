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


@pytest.mark.parametrize(
    ("sql", "params", "expected_sql", "expected_params"),
    [
        ("SELECT $1, $2", ("a", "b"), "SELECT %s, %s", ("a", "b")),
        (
            "WHERE a = $2 AND b = $1",
            ("x", "y"),
            "WHERE a = %s AND b = %s",
            ("y", "x"),
        ),
        ("WHERE a = $1 OR b = $1", ("z",), "WHERE a = %s OR b = %s", ("z", "z")),
        (
            "WHERE id IN ($1, $2, $3)",
            (1, 2, 3),
            "WHERE id IN (%s, %s, %s)",
            (1, 2, 3),
        ),
        ("SELECT $3, $1", ("a", "b", "c"), "SELECT %s, %s", ("c", "a")),
        (
            "CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM 1; END; $$ LANGUAGE plpgsql",
            (),
            "CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM 1; END; $$ LANGUAGE plpgsql",
            (),
        ),
        (
            "CREATE FUNCTION f(x int) RETURNS int AS $$ BEGIN RETURN $1 + 1; END; "
            "$$ LANGUAGE plpgsql",
            ("ignored",),
            "CREATE FUNCTION f(x int) RETURNS int AS $$ BEGIN RETURN $1 + 1; END; "
            "$$ LANGUAGE plpgsql",
            (),
        ),
        (
            "CREATE FUNCTION f(x int) RETURNS int AS $body$ BEGIN RETURN $1 + 1; END; "
            "$body$ LANGUAGE plpgsql",
            ("ignored",),
            "CREATE FUNCTION f(x int) RETURNS int AS $body$ BEGIN RETURN $1 + 1; END; "
            "$body$ LANGUAGE plpgsql",
            (),
        ),
        (
            "INSERT INTO t(name) VALUES ($1) WHERE id = "
            "(SELECT id FROM (SELECT $$abc$$, $2 AS k) s)",
            ("name", 42),
            "INSERT INTO t(name) VALUES (%s) WHERE id = "
            "(SELECT id FROM (SELECT $$abc$$, %s AS k) s)",
            ("name", 42),
        ),
        ("SELECT '$1', $1", ("bound",), "SELECT '$1', %s", ("bound",)),
        (
            "WHERE name = 'O''Brien' AND id = $1",
            (7,),
            "WHERE name = 'O''Brien' AND id = %s",
            (7,),
        ),
        (
            "-- $1 should not bind\nSELECT $1",
            ("bound",),
            "-- $1 should not bind\nSELECT %s",
            ("bound",),
        ),
        (
            "/* $1 should not bind */ SELECT $1",
            ("bound",),
            "/* $1 should not bind */ SELECT %s",
            ("bound",),
        ),
        ('SELECT "$1", $1 FROM t', ("a",), 'SELECT "$1", %s FROM t', ("a",)),
        ("SELECT foo$1, $1 FROM t", ("a",), "SELECT foo$1, %s FROM t", ("a",)),
        (
            "SELECT * FROM task_stages_registry WHERE name = ?",
            ("dev",),
            "SELECT * FROM task_stages_registry WHERE name = %s",
            ("dev",),
        ),
        (
            "SELECT * FROM workflow_definitions WHERE name = ? AND project_id IS ?",
            ("dev", None),
            "SELECT * FROM workflow_definitions WHERE name = %s AND project_id IS %s",
            ("dev", None),
        ),
        (
            "SELECT '?' AS literal WHERE name = ?",
            ("dev",),
            "SELECT '?' AS literal WHERE name = %s",
            ("dev",),
        ),
        ("/* ? */ SELECT ?", ("dev",), "/* ? */ SELECT %s", ("dev",)),
    ],
)
def test_remap_placeholders_to_psycopg(sql, params, expected_sql, expected_params) -> None:
    module = _postgres_module()

    assert module._remap_placeholders_to_psycopg(sql, params) == (
        expected_sql,
        expected_params,
    )


def test_remap_placeholders_to_psycopg_rejects_out_of_range_index() -> None:
    module = _postgres_module()

    with pytest.raises(ValueError, match=r"\$3"):
        module._remap_placeholders_to_psycopg("SELECT $3", ("only",))


def test_remap_placeholders_to_psycopg_rejects_unterminated_dollar_quote() -> None:
    module = _postgres_module()

    with pytest.raises(ValueError, match="unterminated dollar-quote"):
        module._remap_placeholders_to_psycopg("SELECT $body$not closed", ())


class _FakePostgresConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.executemany_calls: list[tuple[str, list[tuple[object, ...]]]] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()):
        self.execute_calls.append((sql, tuple(params)))
        return object()

    def executemany(self, sql: str, rows) -> None:
        self.executemany_calls.append((sql, [tuple(row) for row in rows]))


def test_postgres_transaction_execute_remaps_before_driver_call() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute("SELECT $2, $1, '$3'", ("one", "two", "literal"))

    assert conn.execute_calls == [("SELECT %s, %s, '$3'", ("two", "one"))]


def test_postgres_transaction_execute_remaps_qmark_before_driver_call() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute("SELECT * FROM task_stages_registry WHERE name = ?", ("dev",))

    assert conn.execute_calls == [("SELECT * FROM task_stages_registry WHERE name = %s", ("dev",))]


def test_postgres_transaction_execute_rewrites_sqlite_boolean_literals() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute(
        "SELECT * FROM mcp_servers WHERE enabled = 1 AND graph_synced=0 AND source = 'enabled = 1'"
    )

    assert conn.execute_calls == [
        (
            "SELECT * FROM mcp_servers WHERE enabled = TRUE "
            "AND graph_synced = FALSE AND source = 'enabled = 1'",
            (),
        )
    ]


def test_postgres_transaction_execute_rewrites_boolean_assignment_literals() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute("UPDATE code_indexed_files SET vectors_synced = 1 WHERE id = ?", ("file-1",))

    assert conn.execute_calls == [
        (
            "UPDATE code_indexed_files SET vectors_synced = TRUE WHERE id = %s",
            ("file-1",),
        )
    ]


def test_postgres_transaction_execute_rewrites_sqlite_boolean_coalesce_literals() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute(
        "SELECT * FROM tasks WHERE COALESCE(tasks.is_escalated, 0) = 0 "
        "OR COALESCE(tasks.allow_automation, 1) = 1"
    )

    assert conn.execute_calls == [
        (
            "SELECT * FROM tasks WHERE COALESCE(tasks.is_escalated, FALSE) = FALSE "
            "OR COALESCE(tasks.allow_automation, TRUE) = TRUE",
            (),
        )
    ]


def test_postgres_transaction_execute_coerces_boolean_filter_params() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute(
        "SELECT * FROM workflow_definitions WHERE name = ? AND enabled = ?",
        ("rule", 1),
    )

    assert conn.execute_calls == [
        (
            "SELECT * FROM workflow_definitions WHERE name = %s AND enabled = %s",
            ("rule", True),
        )
    ]


def test_postgres_transaction_execute_coerces_boolean_update_params() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute("UPDATE workflow_definitions SET enabled = ? WHERE id = ?", (0, "wf-1"))

    assert conn.execute_calls == [
        ("UPDATE workflow_definitions SET enabled = %s WHERE id = %s", (False, "wf-1"))
    ]


def test_postgres_transaction_execute_coerces_boolean_insert_params() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute(
        "INSERT INTO workflow_definitions (id, enabled, name) VALUES (?, ?, ?)",
        ("wf-1", 1, "rule"),
    )

    assert conn.execute_calls == [
        (
            "INSERT INTO workflow_definitions (id, enabled, name) VALUES (%s, %s, %s)",
            ("wf-1", True, "rule"),
        )
    ]


def test_postgres_transaction_execute_casts_null_test_params() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute("SELECT * FROM pipeline_executions WHERE (? IS NULL OR status = ?)", (None, None))

    assert conn.execute_calls == [
        (
            "SELECT * FROM pipeline_executions WHERE (%s::text IS NULL OR status = %s)",
            (None, None),
        )
    ]


def test_postgres_safe_update_keeps_qmark_where_style_consistent() -> None:
    module = _postgres_module()

    assert module._build_safe_update(
        "sessions",
        {"message_count": 3, "turn_count": 2},
        "id = ?",
        ("session-1",),
    ) == (
        "UPDATE sessions SET message_count = ?, turn_count = ? WHERE id = ?",
        (3, 2, "session-1"),
    )


def test_postgres_safe_update_shifts_dollar_where_style() -> None:
    module = _postgres_module()

    assert module._build_safe_update(
        "sessions",
        {"message_count": 3, "turn_count": 2},
        "id = $1",
        ("session-1",),
    ) == (
        "UPDATE sessions SET message_count = $1, turn_count = $2 WHERE id = $3",
        (3, 2, "session-1"),
    )


def test_postgres_transaction_executemany_reuses_first_row_rewrite(monkeypatch) -> None:
    module = _postgres_module()
    calls: list[tuple[str, tuple[object, ...]]] = []
    original = module._remap_placeholders_to_psycopg

    def wrapped(sql: str, params):
        calls.append((sql, tuple(params)))
        return original(sql, params)

    monkeypatch.setattr(module, "_remap_placeholders_to_psycopg", wrapped)

    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)
    tx.executemany(
        "INSERT INTO remap_rows (a, b, c) VALUES ($2, $1, $2)",
        [("left", "right"), ("first", "second")],
    )

    assert calls == [
        (
            "INSERT INTO remap_rows (a, b, c) VALUES ($2, $1, $2)",
            ("left", "right"),
        )
    ]
    assert conn.executemany_calls == [
        (
            "INSERT INTO remap_rows (a, b, c) VALUES (%s, %s, %s)",
            [("right", "left", "right"), ("second", "first", "second")],
        )
    ]


def test_postgres_transaction_executemany_reuses_qmark_rewrite() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)
    tx.executemany(
        "INSERT INTO remap_rows (a, b) VALUES (?, ?)",
        [("left", "right"), ("first", "second")],
    )

    assert conn.executemany_calls == [
        (
            "INSERT INTO remap_rows (a, b) VALUES (%s, %s)",
            [("left", "right"), ("first", "second")],
        )
    ]


def test_postgres_transaction_executemany_coerces_boolean_rows() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)
    tx.executemany(
        "INSERT INTO task_stages_registry (name, requires_human, is_terminal) VALUES (?, ?, ?)",
        [("dev", 0, 1), ("merge", 1, 0)],
    )

    assert conn.executemany_calls == [
        (
            "INSERT INTO task_stages_registry (name, requires_human, is_terminal) "
            "VALUES (%s, %s, %s)",
            [("dev", False, True), ("merge", True, False)],
        )
    ]


def test_postgres_transaction_executemany_empty_rows_skips_driver_and_remapper(monkeypatch) -> None:
    module = _postgres_module()

    def fail_if_called(sql: str, params):
        raise AssertionError("remapper should not run for empty executemany rows")

    monkeypatch.setattr(module, "_remap_placeholders_to_psycopg", fail_if_called)

    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)
    tx.executemany("INSERT INTO remap_rows (a) VALUES ($1)", [])

    assert conn.executemany_calls == []


def test_postgres_transaction_executemany_rejects_first_row_out_of_range() -> None:
    module = _postgres_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    with pytest.raises(ValueError, match=r"\$2"):
        tx.executemany("INSERT INTO remap_rows (a) VALUES ($2)", [("only",)])

    assert conn.executemany_calls == []


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def test_postgres_cursor_normalizes_jsonb_values_to_sqlite_contract() -> None:
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


def test_postgres_cursor_normalizes_datetime_values_to_sqlite_contract() -> None:
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
