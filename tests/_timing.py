"""Timing helpers for tests that need async or threaded coordination."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


async def wait_for_async_condition[T](
    predicate: Callable[[], T],
    *,
    timeout: float = 1.0,
    interval: float = 0.001,
    description: str = "condition",
) -> T:
    """Wait until a predicate returns a truthy value, then return it."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if loop.time() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        await asyncio.sleep(min(interval, max(deadline - loop.time(), 0)))


def wait_for_condition[T](
    predicate: Callable[[], T],
    *,
    timeout: float = 1.0,
    interval: float = 0.001,
    description: str = "condition",
) -> T:
    """Synchronous predicate wait for thread/timer based tests."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        time.sleep(min(interval, max(deadline - time.monotonic(), 0)))


async def drain_asyncio_tasks(*, cycles: int = 1) -> None:
    """Yield to the event loop enough times for already-scheduled callbacks to run."""
    for _ in range(cycles):
        await asyncio.sleep(0)


async def wait_forever() -> None:
    """Await forever until cancelled by the code under test."""
    await asyncio.Event().wait()
