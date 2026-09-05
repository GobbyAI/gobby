from __future__ import annotations

import asyncio
import os

import pytest

from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.storage.hub.operation_deadline import database_operation_deadline
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.protocol import Transaction

pytestmark = pytest.mark.integration


def _connection_state(txn: Transaction) -> tuple[int, str, str]:
    row = txn.execute(
        "SELECT pg_backend_pid() AS pid, "
        "current_setting('statement_timeout') AS statement_timeout, "
        "current_setting('lock_timeout') AS lock_timeout"
    ).fetchone()
    assert row is not None
    return int(row["pid"]), str(row["statement_timeout"]), str(row["lock_timeout"])


def test_deadline_settings_do_not_leak_on_ambient_cancellation_or_pool_reuse() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is required for the PostgreSQL pool reuse test")

    database = PostgresHubDatabase(
        dsn,
        pool_config=PostgresPoolConfig(min_size=1, max_size=1),
    )
    try:
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
    finally:
        database.close()

    assert scoped is not None
    assert initial[0] == scoped[0] == restored[0]
    assert initial[1:] == restored[1:]
    for value in scoped[1:]:
        assert value.endswith("ms")
        assert 0 < int(value.removesuffix("ms")) <= 250
