"""Tests for bounded daemon database execution."""

from __future__ import annotations

import asyncio
import threading

import pytest

from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub._ambient import ambient_transaction
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_database_executor_limits_worker_count() -> None:
    """Concurrent work uses no more than max_workers threads."""
    executor = DatabaseExecutor(max_workers=2, thread_name_prefix="test-db")
    start_event = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    thread_ids: set[int] = set()
    workers_started = threading.Event()

    def work(value: int) -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            thread_ids.add(threading.get_ident())
            if active == 2:
                workers_started.set()
        try:
            start_event.wait(timeout=5)
            return value
        finally:
            with lock:
                active -= 1

    try:
        tasks = [asyncio.create_task(executor.run(work, index)) for index in range(8)]
        assert await asyncio.to_thread(workers_started.wait, 1)

        stats = executor.stats()
        assert stats.max_workers == 2
        assert stats.threads <= 2
        assert stats.active <= 2
        assert stats.queued >= 1

        start_event.set()
        assert await asyncio.gather(*tasks) == list(range(8))
        assert max_active <= 2
        assert len(thread_ids) <= 2
    finally:
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_database_executor_does_not_inherit_ambient_transaction(
    temp_db: HubDatabase,
) -> None:
    """Executor work uses its own connection while the caller holds a row lock."""
    executor = DatabaseExecutor(max_workers=1)
    temp_db.execute("DROP TABLE IF EXISTS executor_transaction_isolation")
    temp_db.execute(
        "CREATE TABLE executor_transaction_isolation (id INTEGER PRIMARY KEY, value INTEGER)"
    )
    temp_db.execute("INSERT INTO executor_transaction_isolation VALUES (1, 0)")

    def read_committed_value() -> tuple[bool, int]:
        has_no_ambient_transaction = ambient_transaction(temp_db) is None
        row = temp_db.fetchone(
            "SELECT value FROM executor_transaction_isolation WHERE id = %s",
            (1,),
        )
        assert row is not None
        return has_no_ambient_transaction, int(row["value"])

    try:
        with temp_db.transaction() as txn:
            txn.execute(
                "UPDATE executor_transaction_isolation SET value = %s WHERE id = %s",
                (1, 1),
            )
            assert await asyncio.wait_for(executor.run(read_committed_value), timeout=2) == (
                True,
                0,
            )

        row = temp_db.fetchone(
            "SELECT value FROM executor_transaction_isolation WHERE id = %s", (1,)
        )
        assert row is not None
        assert row["value"] == 1
    finally:
        executor.shutdown(wait=True)
        temp_db.execute("DROP TABLE IF EXISTS executor_transaction_isolation")


@pytest.mark.asyncio
async def test_database_executor_rejects_after_shutdown() -> None:
    """run() rejects work submitted after shutdown."""
    executor = DatabaseExecutor(max_workers=1)
    executor.shutdown(wait=True)

    with pytest.raises(RuntimeError, match="shut down"):
        await executor.run(lambda: 1)
