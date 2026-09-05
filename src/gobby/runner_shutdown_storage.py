"""Cancellation-safe shutdown of daemon-owned database executors."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)
_DATABASE_EXECUTOR_JOIN_SECONDS = 6.0


async def _shutdown_database_executor(
    db_executor: Any,
    *,
    label: str = "Database executor",
    join_thread_name: str = "gobby-db-join",
    join_timeout_seconds: float | None = _DATABASE_EXECUTOR_JOIN_SECONDS,
) -> None:
    """Revoke queued thread work and join bounded operations off-loop."""
    if db_executor.is_joined():
        return
    try:
        db_executor.shutdown(cancel_futures=True)
    except Exception as e:
        logger.warning("%s shutdown failed: %s", label, e)
        return

    loop = asyncio.get_running_loop()
    joined: asyncio.Future[None] = loop.create_future()

    def finish_join(exc: Exception | None = None) -> None:
        if joined.done():
            return
        if exc is None:
            joined.set_result(None)
        else:
            joined.set_exception(exc)

    def join_executor() -> None:
        try:
            db_executor.join()
        except Exception as exc:
            loop.call_soon_threadsafe(finish_join, exc)
        else:
            loop.call_soon_threadsafe(finish_join)

    threading.Thread(target=join_executor, name=join_thread_name, daemon=True).start()
    try:
        if join_timeout_seconds is None:
            await joined
        else:
            await asyncio.wait_for(joined, timeout=join_timeout_seconds)
    except TimeoutError:
        logger.error("%s did not settle before the shutdown deadline", label)
    except Exception as e:
        logger.warning("%s join failed: %s", label, e)


async def _shutdown_database_concurrency(runner: GobbyRunner) -> None:
    watchdog = getattr(runner, "database_watchdog", None)
    if watchdog is not None:
        watchdog.stop()
    worktree_delete_executor = getattr(runner, "worktree_delete_executor", None)
    if worktree_delete_executor is not None:
        await _shutdown_database_executor(
            worktree_delete_executor,
            label="Worktree delete executor",
            join_thread_name="gobby-worktree-delete-join",
            join_timeout_seconds=None,
        )
    coverage_executor = getattr(runner, "coverage_executor", None)
    if coverage_executor is not None:
        await _shutdown_database_executor(
            coverage_executor,
            label="Coverage executor",
            join_thread_name="gobby-coverage-join",
        )
    db_executor = getattr(runner, "db_executor", None)
    if db_executor is not None:
        await _shutdown_database_executor(db_executor)


async def _shutdown_database_concurrency_under_cancellation(
    runner: GobbyRunner,
    cancellation: asyncio.CancelledError | None,
) -> asyncio.CancelledError | None:
    """Defer caller cancellation until database executors are revoked and joined."""
    owned = asyncio.create_task(
        _shutdown_database_concurrency(runner),
        name="database-concurrency-final-shutdown",
    )
    while not owned.done():
        try:
            await asyncio.shield(owned)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    try:
        owned.result()
    except Exception:
        logger.warning("Final database concurrency shutdown failed", exc_info=True)
    return cancellation
