from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from types import SimpleNamespace

import psycopg
import pytest

from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.storage.hub import operation_deadline
from gobby.storage.hub.operation_deadline import (
    DatabaseOperationDeadlineExceeded,
    database_operation_deadline,
    detached_database_operation_deadline,
)
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import Transaction


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
    started = time.monotonic()

    with pytest.raises((DatabaseOperationDeadlineExceeded, psycopg.errors.QueryCanceled)):
        with database_operation_deadline(timeout_seconds=0.12):
            with database.transaction() as txn:
                for _ in range(3):
                    txn.execute("SELECT pg_sleep(0.07)")
                    completed += 1

    assert completed == 1
    assert time.monotonic() - started < 0.20


def test_deadline_applies_when_introduced_inside_ambient_transaction(
    database: PostgresHubDatabase,
) -> None:
    started = time.monotonic()

    with pytest.raises((DatabaseOperationDeadlineExceeded, psycopg.errors.QueryCanceled)):
        with database.transaction() as outer:
            with database_operation_deadline(timeout_seconds=0.04):
                with database.transaction() as nested:
                    assert nested is outer
                    nested.execute("SELECT pg_sleep(0.08)")

    assert time.monotonic() - started < 0.12


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
                "SELECT current_setting('transaction_isolation') AS isolation, "
                "current_setting('transaction_read_only') AS read_only"
            ).fetchone()
            assert row == {"isolation": "repeatable read", "read_only": "on"}


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
