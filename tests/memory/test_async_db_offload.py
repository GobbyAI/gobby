"""Regression tests for synchronous memory storage calls in async paths."""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from unittest.mock import patch

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.memory.manager import MemoryManager

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

async def _assert_event_loop_progresses[T](
    operation: Awaitable[T],
    started: threading.Event,
    release: threading.Event,
) -> T:
    """Prove a deliberately blocked storage call is not running on the event loop."""
    safety_release = threading.Timer(1.0, release.set)
    safety_release.start()
    task = asyncio.ensure_future(operation)
    try:
        observed = await asyncio.wait_for(asyncio.to_thread(started.wait, 0.5), timeout=0.75)
        assert observed, "storage call did not start"
        await asyncio.sleep(0)
        assert not release.is_set(), "storage call blocked the event loop until the safety timeout"
        release.set()
        return await task
    finally:
        release.set()
        safety_release.cancel()
        if not task.done():
            task.cancel()


def _blocking_call[T](
    original: Callable[..., T],
    started: threading.Event,
    release: threading.Event,
) -> Callable[..., T]:
    def call(*args: object, **kwargs: object) -> T:
        started.set()
        release.wait(timeout=1.0)
        return original(*args, **kwargs)

    return call


@pytest.fixture
def memory_manager(hub_db) -> MemoryManager:
    config = MemoryConfig(enabled=True, backend="local", access_debounce_seconds=0)
    return MemoryManager(db=hub_db, config=config)


async def test_recall_access_stats_do_not_block_event_loop(memory_manager: MemoryManager) -> None:
    memory = await memory_manager.create_memory("Remember the async recall path")
    started = threading.Event()
    release = threading.Event()
    storage = memory_manager.storage

    with patch.object(
        storage,
        "update_access_stats",
        side_effect=_blocking_call(storage.update_access_stats, started, release),
    ):
        await _assert_event_loop_progresses(
            memory_manager._update_access_stats([memory]), started, release
        )


async def test_delete_does_not_block_event_loop(memory_manager: MemoryManager) -> None:
    memory = await memory_manager.create_memory("Delete without blocking")
    started = threading.Event()
    release = threading.Event()
    storage = memory_manager.storage

    with patch.object(
        storage,
        "delete_memory",
        side_effect=_blocking_call(storage.delete_memory, started, release),
    ):
        deleted = await _assert_event_loop_progresses(
            memory_manager.delete_memory(memory.id), started, release
        )

    assert deleted is True


async def test_update_does_not_block_event_loop(memory_manager: MemoryManager) -> None:
    memory = await memory_manager.create_memory("Update without blocking")
    started = threading.Event()
    release = threading.Event()
    storage = memory_manager.storage

    with patch.object(
        storage,
        "update_memory",
        side_effect=_blocking_call(storage.update_memory, started, release),
    ):
        updated = await _assert_event_loop_progresses(
            memory_manager.update_memory(memory.id, tags=["offloaded"]), started, release
        )

    assert updated.tags == ["offloaded"]


async def test_graph_enqueue_does_not_block_event_loop(memory_manager: MemoryManager) -> None:
    memory = await memory_manager.create_memory("Queue without blocking")
    started = threading.Event()
    release = threading.Event()
    storage = memory_manager.storage

    with patch.object(
        storage,
        "mark_pending_graph",
        side_effect=_blocking_call(storage.mark_pending_graph, started, release),
    ):
        await _assert_event_loop_progresses(
            memory_manager._enqueue_for_graph(memory.id), started, release
        )
