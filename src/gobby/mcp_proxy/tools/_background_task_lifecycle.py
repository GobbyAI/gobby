"""Shared lifecycle supervision for fire-and-forget internal-tool work."""

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_internal_tool_loop: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "internal_tool_background_loop",
    default=None,
)


@contextmanager
def internal_tool_background_loop(loop: asyncio.AbstractEventLoop) -> Iterator[None]:
    """Expose the caller's event loop to synchronous internal-tool worker threads."""
    token = _internal_tool_loop.set(loop)
    try:
        yield
    finally:
        _internal_tool_loop.reset(token)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def resolve_background_loop() -> asyncio.AbstractEventLoop | None:
    """The loop fire-and-forget work should run on, from a thread or the loop.

    A synchronous internal tool executes in a worker thread, where there is no
    running loop even though the daemon has one; `internal_tool_background_loop`
    is how the caller hands it over. Returns None when nothing usable is
    reachable, which is the standalone case -- a CLI, a test -- and the caller
    decides what to do about it.
    """
    target = _running_loop() or _internal_tool_loop.get()
    if target is None or target.is_closed():
        return None
    return target


def register_background_task(
    registry: dict[str, asyncio.Task[None]],
    key: str,
    task: asyncio.Task[None],
    *,
    logger: logging.Logger,
    description: str,
) -> None:
    """Retain a task until completion, then consume and report its exception."""
    registry[key] = task

    def _on_done(done_task: asyncio.Task[None]) -> None:
        current = registry.get(key)
        if current is done_task:
            registry.pop(key, None)
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is not None:
            logger.error(
                "%s %s failed: %s",
                description,
                key,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    task.add_done_callback(_on_done)


def schedule_background_task(
    registry: dict[str, asyncio.Task[None]],
    key_prefix: str,
    coroutine_factory: Callable[[], Coroutine[Any, Any, None]],
    *,
    name: str,
    logger: logging.Logger,
    description: str,
) -> None:
    """Create and register a task on the current or propagated internal-tool loop."""
    current_loop = _running_loop()
    target_loop = resolve_background_loop()
    if target_loop is None:
        raise RuntimeError("no running event loop available for background task")

    def _schedule() -> None:
        task = target_loop.create_task(coroutine_factory(), name=name)
        register_background_task(
            registry,
            f"{key_prefix}:{id(task)}",
            task,
            logger=logger,
            description=description,
        )

    if current_loop is target_loop:
        _schedule()
    else:
        target_loop.call_soon_threadsafe(_schedule)
