from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.storage.hub import operation_deadline, postgres_pool
from gobby.storage.hub.operation_deadline import (
    DatabaseOperationDeadlineExceeded,
    database_operation_deadline,
    detached_database_operation_deadline,
)
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import Transaction
from gobby.utils.datetime import to_json_safe


@pytest.fixture
def database() -> Iterator[PostgresHubDatabase]:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is required for the PostgreSQL deadline tests")

    database = PostgresHubDatabase(
        dsn,
        pool_config=PostgresPoolConfig(min_size=1, max_size=1),
    )
    try:
        # Open the pooled connection before any deadline starts, so the tight
        # per-statement budgets below measure statements rather than connect cost.
        with database.transaction() as txn:
            txn.execute("SELECT 1")
        yield database
    finally:
        database.close()


pytestmark = pytest.mark.integration


def _connection_state(txn: Transaction) -> tuple[int, str, str]:
    row = txn.execute(
        "SELECT pg_backend_pid() AS pid, "
        "current_setting('statement_timeout') AS statement_timeout, "
        "current_setting('lock_timeout') AS lock_timeout"
    ).fetchone()
    assert row is not None
    return int(row["pid"]), str(row["statement_timeout"]), str(row["lock_timeout"])


def test_deadline_bounds_every_statement(database: PostgresHubDatabase) -> None:
    completed = 0

    with pytest.raises((DatabaseOperationDeadlineExceeded, psycopg.errors.QueryCanceled)):
        with database_operation_deadline(timeout_seconds=0.12):
            with database.transaction() as txn:
                for _ in range(3):
                    txn.execute("SELECT pg_sleep(0.07)")
                    completed += 1

    # The shared budget must interrupt the second statement. Count completed
    # work rather than client scheduling and transaction cleanup time.
    assert completed == 1


def test_deadline_applies_when_introduced_inside_ambient_transaction(
    database: PostgresHubDatabase,
) -> None:
    with pytest.raises((DatabaseOperationDeadlineExceeded, psycopg.errors.QueryCanceled)):
        with database.transaction() as outer:
            with database_operation_deadline(timeout_seconds=0.04):
                with database.transaction() as nested:
                    assert nested is outer
                    nested.execute("SELECT pg_sleep(0.08)")


def test_executemany_refreshes_the_deadline_between_rows(database: PostgresHubDatabase) -> None:
    with pytest.raises((DatabaseOperationDeadlineExceeded, psycopg.errors.QueryCanceled)):
        with database_operation_deadline(timeout_seconds=0.12):
            with database.transaction() as txn:
                txn.executemany("SELECT pg_sleep(%s)", [(0.07,)] * 3)


def test_compound_query_is_rejected_before_any_statement_runs(
    database: PostgresHubDatabase,
) -> None:
    database.execute("CREATE TEMP SEQUENCE rejected_batch_probe")
    with pytest.raises(psycopg.errors.SyntaxError, match="multiple commands"):
        with database_operation_deadline(timeout_seconds=0.12):
            with database.transaction() as txn:
                txn.execute(
                    "SELECT nextval('rejected_batch_probe'); "
                    "SELECT pg_sleep(.07); SELECT pg_sleep(.07); SELECT pg_sleep(.07)"
                )
    # Sequence increments survive rollback, so this proves pre-execution rejection.
    assert database.fetchone("SELECT is_called FROM rejected_batch_probe") == {"is_called": False}


def test_bounded_statements_allow_quoted_semicolons_and_do_bodies(
    database: PostgresHubDatabase,
) -> None:
    with database_operation_deadline(timeout_seconds=0.4):
        with database.transaction() as txn:
            assert txn.execute("SELECT '; %s' AS literal").fetchone() == {"literal": "; %s"}
            txn.execute("DO $$ BEGIN PERFORM 1; PERFORM 2; END $$")
            assert txn.execute("SELECT 3 AS value; -- trailing semicolon").fetchone() == {
                "value": 3
            }


def test_savepoint_recovery_after_bounded_statement_timeout(database: PostgresHubDatabase) -> None:
    with database_operation_deadline(timeout_seconds=0.4, operation_timeout_seconds=0.03):
        with database.transaction() as txn:
            savepoint = txn.savepoint("before_timeout")
            with pytest.raises(psycopg.errors.QueryCanceled):
                txn.execute("SELECT pg_sleep(.06)")
            savepoint.rollback()
            assert txn.execute("SELECT 1 AS value").fetchone() == {"value": 1}
            savepoint.release()


@pytest.mark.parametrize("scope_kind", ["deadline", "bounded_transaction"])
def test_savepoint_rollback_does_not_restore_exited_scope_timeouts(
    database: PostgresHubDatabase, scope_kind: str
) -> None:
    with database.transaction() as txn:
        initial = _connection_state(txn)
        if scope_kind == "deadline":
            with database_operation_deadline(timeout_seconds=0.4):
                savepoint = txn.savepoint("inside_scope")
        else:
            with database.bounded_transaction(statement_timeout_ms=100, lock_timeout_ms=50):
                savepoint = txn.savepoint("inside_scope")
        assert _connection_state(txn) == initial
        savepoint.rollback()
        assert _connection_state(txn) == initial


def test_definite_commit_rejection_preserves_driver_error(database: PostgresHubDatabase) -> None:
    database.execute(
        "CREATE TEMP TABLE definite_commit_rejection "
        "(value INTEGER UNIQUE DEFERRABLE INITIALLY DEFERRED)"
    )
    callbacks: list[str] = []
    with pytest.raises(psycopg.errors.UniqueViolation):
        with database.transaction() as txn:
            txn.execute("INSERT INTO definite_commit_rejection VALUES (1), (1)")
            txn.after_commit(lambda: callbacks.append("committed"))
    assert database.fetchone("SELECT count(*) AS count FROM definite_commit_rejection") == {
        "count": 0
    }
    assert callbacks == []


def test_swallowed_statement_error_raises_instead_of_silent_rollback(
    database: PostgresHubDatabase,
) -> None:
    """PostgreSQL answers COMMIT of an aborted transaction with ROLLBACK (#23296)."""
    database.execute("CREATE TEMP TABLE swallowed_statement_error (value INTEGER UNIQUE)")
    callbacks: list[str] = []
    with pytest.raises(psycopg.errors.InFailedSqlTransaction):
        with database.transaction() as txn:
            txn.execute("INSERT INTO swallowed_statement_error VALUES (1)")
            txn.after_commit(lambda: callbacks.append("committed"))
            with pytest.raises(psycopg.errors.UniqueViolation):
                txn.execute("INSERT INTO swallowed_statement_error VALUES (1)")
    assert database.fetchone("SELECT count(*) AS count FROM swallowed_statement_error") == {
        "count": 0
    }
    assert callbacks == []


@pytest.mark.parametrize("operation", ["execute", "executemany", "savepoint", "release"])
def test_expired_scope_prevents_further_operations(
    database: PostgresHubDatabase, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.monotonic()
    monkeypatch.setattr(operation_deadline, "time", SimpleNamespace(monotonic=lambda: now))
    with pytest.raises(DatabaseOperationDeadlineExceeded):
        with database_operation_deadline(timeout_seconds=0.04):
            with database.transaction() as txn:
                savepoint = txn.savepoint("before_expiry")
                now += 0.06
                if operation == "execute":
                    txn.execute("SELECT 1")
                elif operation == "executemany":
                    txn.executemany("SELECT %s", [(1,)])
                elif operation == "savepoint":
                    txn.savepoint("after_expiry")
                else:
                    savepoint.release()
                pytest.fail("Operation ran after the deadline")


def test_existing_stricter_timeouts_are_preserved(database: PostgresHubDatabase) -> None:
    with database.transaction() as txn:
        txn.execute("SET LOCAL statement_timeout = '20ms'")
        txn.execute("SET LOCAL lock_timeout = '10ms'")
        with database_operation_deadline(timeout_seconds=0.4):
            scoped = _connection_state(txn)
        restored = _connection_state(txn)
    assert scoped[1:] == restored[1:] == ("20ms", "10ms")


def _record_statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    statements: list[str] = []
    real_execute = psycopg.Connection.execute

    def recording_execute(
        self: psycopg.Connection[Any], query: Any, *args: Any, **kwargs: Any
    ) -> psycopg.Cursor[Any]:
        statements.append(query if isinstance(query, str) else repr(query))
        return real_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", recording_execute)
    return statements


def test_deadline_reads_the_session_timeouts_once_per_connection(
    database: PostgresHubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SHOW pair cost ~125 us of GIL time per deadline transaction (#23359)."""
    statements = _record_statements(monkeypatch)
    states = []
    with database_operation_deadline(timeout_seconds=0.4):
        for _ in range(3):
            with database.transaction() as txn:
                states.append(_connection_state(txn))

    assert statements.count("SHOW statement_timeout") == 1
    assert statements.count("SHOW lock_timeout") == 1
    assert len({pid for pid, _, _ in states}) == 1
    assert all(
        0 < int(value.removesuffix("ms")) <= 400 for _, *values in states for value in values
    )


def test_a_session_timeout_change_is_read_again_by_later_transactions(
    database: PostgresHubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _record_statements(monkeypatch)
    with database_operation_deadline(timeout_seconds=600):
        with database.transaction() as txn:
            _connection_state(txn)
        with database.transaction() as txn:
            # Session level, below the deadline's 5 s per-operation cap.
            txn.execute("SET statement_timeout = '3s'")
        with database.transaction() as txn:
            _, statement_timeout, lock_timeout = _connection_state(txn)

    assert statement_timeout == "3s"
    assert lock_timeout != "3s"
    assert statements.count("SHOW statement_timeout") == 2


def test_bounded_transaction_composes_with_deadline_and_restores_outer_settings(
    database: PostgresHubDatabase,
) -> None:
    with database.transaction() as txn:
        initial = _connection_state(txn)
        with database_operation_deadline(timeout_seconds=0.4):
            with database.bounded_transaction(statement_timeout_ms=100, lock_timeout_ms=50):
                bounded = _connection_state(txn)
                with database.bounded_transaction(statement_timeout_ms=20, lock_timeout_ms=10):
                    nested = _connection_state(txn)
                restored_bounded = _connection_state(txn)
            deadline_only = _connection_state(txn)
        restored = _connection_state(txn)
    assert bounded[1:] == restored_bounded[1:] == ("100ms", "50ms")
    assert nested[1:] == ("20ms", "10ms")
    assert all(100 < int(value.removesuffix("ms")) <= 400 for value in deadline_only[1:])
    assert initial == restored


def test_deadline_configuration_allows_repeatable_read_before_first_query(
    database: PostgresHubDatabase,
) -> None:
    with database_operation_deadline(timeout_seconds=0.4):
        with database.bounded_transaction(repeatable_read_read_only=True) as txn:
            row = txn.execute(
                "SELECT current_setting('transaction_isolation') AS checkout_mode, "
                "current_setting('transaction_read_only') AS read_only"
            ).fetchone()
            assert row == {"checkout_mode": "repeatable read", "read_only": "on"}


def test_nested_deadline_settings_restore_to_each_owning_scope(
    database: PostgresHubDatabase,
) -> None:
    with database.transaction() as txn:
        initial = _connection_state(txn)
        with database_operation_deadline(timeout_seconds=0.4):
            outer = _connection_state(txn)
            with database_operation_deadline(timeout_seconds=0.1):
                inner = _connection_state(txn)
            restored_outer = _connection_state(txn)
        restored_initial = _connection_state(txn)

    assert initial[1:] == restored_initial[1:]
    assert all(value.endswith("ms") for value in outer[1:])
    assert all(value.endswith("ms") for value in inner[1:])
    assert all(value.endswith("ms") for value in restored_outer[1:])
    assert all(0 < int(value.removesuffix("ms")) <= 100 for value in inner[1:])
    assert all(100 < int(value.removesuffix("ms")) <= 400 for value in restored_outer[1:])


def test_detached_deadline_ignores_an_exhausted_inherited_scope(
    database: PostgresHubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.monotonic()
    monkeypatch.setattr(operation_deadline, "time", SimpleNamespace(monotonic=lambda: now))

    with database_operation_deadline(timeout_seconds=0.04):
        now += 0.06
        with pytest.raises(DatabaseOperationDeadlineExceeded):
            database.fetchone("SELECT 1 AS value")
        with detached_database_operation_deadline(timeout_seconds=5):
            assert database.fetchone("SELECT 1 AS value") == {"value": 1}
        # The exhausted inherited deadline is restored on exit.
        with pytest.raises(DatabaseOperationDeadlineExceeded):
            database.fetchone("SELECT 1 AS value")


def test_detached_deadline_bounds_its_own_window(
    database: PostgresHubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.monotonic()
    monkeypatch.setattr(operation_deadline, "time", SimpleNamespace(monotonic=lambda: now))

    with detached_database_operation_deadline(timeout_seconds=0.04):
        now += 0.06
        with pytest.raises(DatabaseOperationDeadlineExceeded):
            database.fetchone("SELECT 1 AS value")


def test_expired_deadline_rolls_back_before_commit(
    database: PostgresHubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.monotonic()
    monkeypatch.setattr(operation_deadline, "time", SimpleNamespace(monotonic=lambda: now))
    database.execute("CREATE TEMP TABLE deadline_commit_probe (value INTEGER)")
    callbacks: list[str] = []

    with pytest.raises(DatabaseOperationDeadlineExceeded):
        with database_operation_deadline(timeout_seconds=0.04):
            with database.transaction() as txn:
                txn.execute("INSERT INTO deadline_commit_probe VALUES (1)")
                txn.after_commit(lambda: callbacks.append("committed"))
                now += 0.06

    row = database.fetchone("SELECT count(*) AS count FROM deadline_commit_probe")
    assert row is not None
    assert row["count"] == 0
    assert callbacks == []


def test_deadline_settings_do_not_leak_on_ambient_cancellation_or_pool_reuse(
    database: PostgresHubDatabase,
) -> None:
    with database.transaction() as txn:
        initial = _connection_state(txn)

    scoped: tuple[int, str, str] | None = None
    with pytest.raises(asyncio.CancelledError):
        with database_operation_deadline(timeout_seconds=0.25):
            with database.transaction() as outer:
                with database.transaction() as nested:
                    assert nested is outer
                    scoped = _connection_state(nested)
                raise asyncio.CancelledError

    with database.transaction() as txn:
        restored = _connection_state(txn)

    assert scoped is not None
    assert initial[0] == scoped[0] == restored[0]
    assert initial[1:] == restored[1:]
    for value in scoped[1:]:
        assert value.endswith("ms")
        assert 0 < int(value.removesuffix("ms")) <= 250


def test_distant_deadline_fits_postgres_timeout_range(database: PostgresHubDatabase) -> None:
    with database.transaction() as txn:
        txn.execute("SET LOCAL statement_timeout = 0; SET LOCAL lock_timeout = 0")
        with database_operation_deadline(timeout_seconds=10**10, operation_timeout_seconds=10**10):
            row = txn.execute("SELECT current_setting('statement_timeout') AS value").fetchone()
            assert row is not None
            assert row["value"] == "2147483647ms"


# True only when the server opened no transaction block before this statement:
# an explicit BEGIN fixes now() at its own, earlier, statement start.
_NO_EARLIER_BEGIN = "SELECT now() = statement_timestamp() AS fresh"


@pytest.fixture
def opened_transactions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []
    original = psycopg.Connection.transaction

    def recording(self: psycopg.Connection[Any], *args: Any, **kwargs: Any) -> Any:
        opened.append("BEGIN")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "transaction", recording)
    return opened


def test_lone_reads_run_in_autocommit_without_a_transaction_block(
    database: PostgresHubDatabase,
    opened_transactions: list[str],
) -> None:
    assert database.fetchone(_NO_EARLIER_BEGIN) == {"fresh": True}
    assert database.fetchall("SELECT %s::int AS n", (7,)) == [{"n": 7}]
    assert database.execute("WITH v AS (SELECT 2 AS n) SELECT n FROM v").fetchall() == [{"n": 2}]

    assert opened_transactions == []
    with database._pool.connection() as connection:
        assert connection.autocommit is False


def test_failed_autocommit_read_returns_the_connection_transactional(
    database: PostgresHubDatabase,
) -> None:
    with pytest.raises(psycopg.errors.DivisionByZero):
        database.fetchone("SELECT 1 / 0")

    with database._pool.connection() as connection:
        assert connection.autocommit is False
    assert database.fetchone("SELECT 3 AS n") == {"n": 3}


def test_writes_ambient_reads_and_deadline_reads_stay_transactional(
    database: PostgresHubDatabase,
    opened_transactions: list[str],
) -> None:
    database.execute("CREATE TEMP TABLE decision_seven (n int)")
    inserted = database.fetchone(
        "INSERT INTO decision_seven VALUES (1) RETURNING now() = statement_timestamp() AS fresh"
    )
    assert inserted == {"fresh": False}

    with database.transaction() as txn:
        txn.execute("INSERT INTO decision_seven VALUES (2)")
        # The pool holds one connection, so only the ambient transaction can
        # answer this read, and it sees the uncommitted row.
        assert database.fetchone("SELECT count(*) AS n FROM decision_seven") == {"n": 2}
        assert database.fetchone(_NO_EARLIER_BEGIN) == {"fresh": False}

    # Deadline bounds are SET LOCAL, which only a transaction block honors.
    with database_operation_deadline(timeout_seconds=5):
        assert database.fetchone(_NO_EARLIER_BEGIN) == {"fresh": False}

    assert len(opened_transactions) == 4


_TYPED_ROW = (
    'SELECT \'{"b":1,"a":[1]}\'::jsonb AS doc, \'[1, {"z": null}]\'::json AS raw, '
    '\'{"é": 1.50, "aa": "ü", "n": 10000000000000000000}\'::jsonb AS wide, '
    "'5'::jsonb AS n, '\"x\"'::jsonb AS s, 'null'::jsonb AS z, NULL::jsonb AS missing, "
    "'0E8A1C5E-2B7D-4F00-9C1A-3D2E1F0A9B8C'::uuid AS id"
)
_TYPED_VALUES = {
    "doc": '{"a":[1],"b":1}',
    "raw": '[1,{"z":null}]',
    "wide": '{"aa":"\\u00fc","n":10000000000000000000,"\\u00e9":1.5}',
    "n": 5,
    "s": "x",
    "z": None,
    "missing": None,
    "id": "0e8a1c5e-2b7d-4f00-9c1a-3d2e1f0a9b8c",
}


def test_pool_connections_return_the_canonical_json_text_rows_always_had(
    database: PostgresHubDatabase,
) -> None:
    """Containers keep the sorted compact form that stored hashes and edit checks
    compare (stage registry row_hash), now without the to_json_safe walk (#23359)."""
    dsn = os.environ["DATABASE_URL"]
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as default_loaders:
        before = default_loaders.execute(_TYPED_ROW).fetchone()
    with database._pool_connection() as connection:
        row = connection.execute(_TYPED_ROW).fetchone()

    assert before is not None
    assert row == {key: postgres_pool._normalize_value(value) for key, value in before.items()}
    assert row == _TYPED_VALUES


def test_hub_reads_canonicalize_json_containers_without_the_safe_value_walk(
    database: PostgresHubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    walked: list[object] = []

    def spy_to_json_safe(value: object) -> object:
        walked.append(value)
        return to_json_safe(value)

    monkeypatch.setattr("gobby.storage.hub.postgres_pool.to_json_safe", spy_to_json_safe)

    assert database.fetchone(_TYPED_ROW) == _TYPED_VALUES
    with database.transaction() as txn:
        assert txn.execute(_TYPED_ROW).fetchall() == [_TYPED_VALUES]

    # Decoded jsonb holds only JSON-native values, so the walk had nothing to convert.
    assert walked == []


@pytest.mark.parametrize(
    ("sql", "eligible"),
    [
        ("SELECT 1", True),
        ("  select 1;", True),
        ("WITH v AS (SELECT 1) SELECT * FROM v", True),
        ("WITH gone AS (DELETE FROM t RETURNING id) SELECT id FROM gone", False),
        ("WITH moved AS (UPDATE t SET n = 1 RETURNING id) SELECT id FROM moved", False),
        ("SELECT 1; SELECT 2", False),
        ("SET statement_timeout = 0", False),
        ("SHOW statement_timeout", False),
        ("SAVEPOINT before_write", False),
        ("INSERT INTO t VALUES (1)", False),
    ],
)
def test_only_single_read_statements_qualify_for_autocommit(sql: str, eligible: bool) -> None:
    assert postgres_pool.is_autocommit_read(sql) is eligible
