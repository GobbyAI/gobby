"""Strong-reference tracking for fire-and-forget hook tasks."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

_background_tasks: set[asyncio.Task[Any]] = set()
_background_tasks_lock = threading.Lock()


def create_background_task[T](
    coro: Coroutine[Any, Any, T],
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.Task[T]:
    """Create and strongly retain a task until its completion callback runs."""
    active_loop = loop if loop is not None else asyncio.get_running_loop()
    task = active_loop.create_task(coro)
    with _background_tasks_lock:
        _background_tasks.add(task)
    task.add_done_callback(_discard_background_task)
    return task


def _discard_background_task(task: asyncio.Task[Any]) -> None:
    with _background_tasks_lock:
        _background_tasks.discard(task)
