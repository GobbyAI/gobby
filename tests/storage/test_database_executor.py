"""Tests for bounded daemon database execution."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

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
async def test_database_executor_rejects_after_shutdown() -> None:
    """run() rejects work submitted after shutdown."""
    executor = DatabaseExecutor(max_workers=1)
    executor.shutdown(wait=True)

    with pytest.raises(RuntimeError, match="shut down"):
        await executor.run(lambda: 1)
