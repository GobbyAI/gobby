"""Tests for bounded daemon database execution."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.executor import DatabaseExecutor

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

    def work(value: int) -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            thread_ids.add(threading.get_ident())
        try:
            start_event.wait(timeout=5)
            time.sleep(0.02)
            return value
        finally:
            with lock:
                active -= 1

    try:
        tasks = [asyncio.create_task(executor.run(work, index)) for index in range(8)]
        await asyncio.sleep(0.05)

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
async def test_database_executor_bounds_localdatabase_connections(temp_dir) -> None:
    """SQLite handles stay near the configured worker count under concurrent load."""
    db = LocalDatabase(temp_dir / "executor_connections.db")
    db.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    executor = DatabaseExecutor(max_workers=2, thread_name_prefix="test-db")
    start_event = threading.Event()
    observed_counts: list[int] = []
    lock = threading.Lock()

    def query() -> int:
        start_event.wait(timeout=5)
        row = db.fetchone("SELECT 1 AS value")
        with lock:
            observed_counts.append(db.connection_count)
        return int(row["value"]) if row else 0

    try:
        tasks = [asyncio.create_task(executor.run(query)) for _ in range(20)]
        await asyncio.sleep(0.05)
        start_event.set()
        assert await asyncio.gather(*tasks) == [1] * 20
        assert max(observed_counts) <= 3  # main thread connection + two executor workers
    finally:
        executor.shutdown(wait=True)
        db.close()


@pytest.mark.asyncio
async def test_database_executor_rejects_after_shutdown() -> None:
    """run() rejects work submitted after shutdown."""
    executor = DatabaseExecutor(max_workers=1)
    executor.shutdown(wait=True)

    with pytest.raises(RuntimeError, match="shut down"):
        await executor.run(lambda: 1)
