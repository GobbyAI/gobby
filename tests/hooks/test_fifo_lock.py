"""CrossLoopFifoLock ordering, cross-thread handoff, and cancellation contracts."""

from __future__ import annotations

import asyncio

import pytest

from gobby.hooks.fifo_lock import CrossLoopFifoLock
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit


async def _record_turn(lock: CrossLoopFifoLock, order: list[str], name: str) -> None:
    await lock.acquire()
    order.append(name)
    lock.release()


@pytest.mark.asyncio
async def test_release_passes_the_lock_to_queued_waiters_before_newcomers() -> None:
    lock = CrossLoopFifoLock()
    order: list[str] = []
    await lock.acquire()
    queued = [asyncio.create_task(_record_turn(lock, order, name)) for name in ("first", "second")]
    await drain_asyncio_tasks()
    assert order == []

    lock.release()
    newcomer = asyncio.create_task(_record_turn(lock, order, "newcomer"))
    await asyncio.gather(*queued, newcomer)

    assert order == ["first", "second", "newcomer"]
    assert not lock.locked()


@pytest.mark.asyncio
async def test_release_on_another_thread_wakes_the_waiter_on_its_loop() -> None:
    lock = CrossLoopFifoLock()
    await asyncio.to_thread(asyncio.run, lock.acquire())
    waiter = asyncio.create_task(lock.acquire())
    await drain_asyncio_tasks()
    assert not waiter.done()

    await asyncio.to_thread(lock.release)

    await asyncio.wait_for(waiter, timeout=1)
    assert lock.locked()
    lock.release()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cancelled_queued_waiter_leaves_the_queue() -> None:
    lock = CrossLoopFifoLock()
    await lock.acquire()
    abandoned = asyncio.create_task(lock.acquire())
    await drain_asyncio_tasks()

    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    lock.release()

    assert not lock.locked()


@pytest.mark.parametrize("grant_ran", [False, True], ids=["before-grant", "after-grant"])
@pytest.mark.asyncio
async def test_waiter_cancelled_during_handoff_passes_the_lock_on(grant_ran: bool) -> None:
    lock = CrossLoopFifoLock()
    await lock.acquire()
    cancelled = asyncio.create_task(lock.acquire())
    successor = asyncio.create_task(lock.acquire())
    await drain_asyncio_tasks()

    lock.release()
    if grant_ran:
        await drain_asyncio_tasks()
    cancelled.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await asyncio.wait_for(successor, timeout=1)
    assert lock.locked()
    lock.release()
    assert not lock.locked()
