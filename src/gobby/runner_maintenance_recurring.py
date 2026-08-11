"""Recurring daemon maintenance loops with staggered startup scheduling."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.config.runtime import RuntimeActiveBundle
    from gobby.mcp_proxy.metrics import ToolMetricsManager

# Preserve the public maintenance logger used before these loops were extracted.
logger = logging.getLogger("gobby.runner_maintenance")

_MAINTENANCE_STARTUP_WINDOW_SECONDS = 5 * 60


async def _run_sync_maintenance(
    run_db: Callable[..., Awaitable[Any]] | None,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run synchronous maintenance work away from the daemon event loop."""
    if run_db is not None:
        return await run_db(func, *args, **kwargs)
    return await asyncio.to_thread(func, *args, **kwargs)


def _deterministic_startup_delay(name: str, *, window_seconds: int) -> float:
    """Return a stable offset in ``[1, window_seconds]`` for a maintenance loop."""
    if window_seconds <= 0:
        return 0.0
    digest = hashlib.blake2s(name.encode(), digest_size=4).digest()
    return float(1 + (int.from_bytes(digest) % window_seconds))


async def _wait_for_first_maintenance_cycle(
    name: str,
    is_shutdown_requested: Callable[[], bool],
    *,
    startup_delay_seconds: float | None,
    sleep: Callable[[float], Awaitable[None]],
) -> bool:
    """Wait for the bounded startup offset and report whether work should begin."""
    if is_shutdown_requested():
        return False
    delay = (
        _deterministic_startup_delay(name, window_seconds=_MAINTENANCE_STARTUP_WINDOW_SECONDS)
        if startup_delay_seconds is None
        else max(0.0, min(float(startup_delay_seconds), _MAINTENANCE_STARTUP_WINDOW_SECONDS))
    )
    if delay <= 0:
        return True
    try:
        await sleep(delay)
    except asyncio.CancelledError:
        return False
    return not is_shutdown_requested()


async def metrics_cleanup_loop(
    metrics_manager: ToolMetricsManager,
    is_shutdown_requested: Callable[[], bool],
    *,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    interval_seconds: int = 24 * 60 * 60,
    startup_delay_seconds: float | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Clean old metrics shortly after startup, then at the configured interval."""
    sleep_fn = sleep or asyncio.sleep
    if not await _wait_for_first_maintenance_cycle(
        "metrics-cleanup",
        is_shutdown_requested,
        startup_delay_seconds=startup_delay_seconds,
        sleep=sleep_fn,
    ):
        return

    while True:
        try:
            deleted = await _run_sync_maintenance(
                run_db,
                metrics_manager.cleanup_old_metrics,
            )
            if deleted > 0:
                logger.info("Periodic metrics cleanup: removed %s old entries", deleted)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in metrics cleanup loop: %s", e)
        try:
            await sleep_fn(interval_seconds)
        except asyncio.CancelledError:
            break
        if is_shutdown_requested():
            break


async def tool_result_cleanup_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    *,
    capture_bundle: Callable[[], RuntimeActiveBundle],
    run_db: Callable[..., Awaitable[Any]] | None = None,
    interval_seconds: int = 24 * 60 * 60,
    startup_delay_seconds: float | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Clean expired offloaded tool results once per day."""
    from gobby.storage.tool_results import ToolResultStore

    sleep_fn = sleep or asyncio.sleep
    if not await _wait_for_first_maintenance_cycle(
        "tool-result-cleanup",
        is_shutdown_requested,
        startup_delay_seconds=startup_delay_seconds,
        sleep=sleep_fn,
    ):
        return

    while True:
        config = capture_bundle().snapshot.active
        store = ToolResultStore(db, config.get_tool_result_offload_config())
        try:
            deleted = await _run_sync_maintenance(run_db, store.cleanup_expired)
            if deleted > 0:
                logger.info("Periodic tool-result cleanup: removed %s expired results", deleted)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error in tool-result cleanup loop: %s", exc)
        try:
            await sleep_fn(interval_seconds)
        except asyncio.CancelledError:
            break
        if is_shutdown_requested():
            break


async def metrics_archive_loop(
    event_store: Any,
    is_shutdown_requested: Callable[[], bool],
    retention_days: int = 30,
    *,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    interval_seconds: int = 24 * 60 * 60,
    startup_delay_seconds: float | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Archive metrics shortly after startup, then at the configured interval."""
    sleep_fn = sleep or asyncio.sleep
    if not await _wait_for_first_maintenance_cycle(
        "metrics-archive",
        is_shutdown_requested,
        startup_delay_seconds=startup_delay_seconds,
        sleep=sleep_fn,
    ):
        return

    while True:
        try:
            archived = await _run_sync_maintenance(
                run_db,
                event_store.archive_old_events,
                retention_days=retention_days,
            )
            if archived > 0:
                logger.info(
                    "Metrics archive: rolled up %s events older than %s days",
                    archived,
                    retention_days,
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in metrics archive loop: %s", e)
        try:
            await sleep_fn(interval_seconds)
        except asyncio.CancelledError:
            break
        if is_shutdown_requested():
            break


async def memory_reconcile_loop(
    memory_manager: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 24 * 60 * 60,
    *,
    startup_delay_seconds: float | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Reconcile memory stores shortly after startup, then at the configured interval."""
    sleep_fn = sleep or asyncio.sleep
    if not await _wait_for_first_maintenance_cycle(
        "memory-reconcile",
        is_shutdown_requested,
        startup_delay_seconds=startup_delay_seconds,
        sleep=sleep_fn,
    ):
        return

    while True:
        try:
            report = await memory_manager.reconcile_stores(dry_run=False)
            qdrant_orphans = report.get("qdrant", {}).get("orphans_deleted", 0)
            falkordb_orphans = report.get("falkordb", {}).get("orphan_memories_deleted", 0)
            falkordb_entities = report.get("falkordb", {}).get("orphan_entities_deleted", 0)
            if qdrant_orphans or falkordb_orphans or falkordb_entities:
                logger.info(
                    "Memory reconciliation: %s Qdrant orphans, %s FalkorDB memory orphans, "
                    "%s FalkorDB entity orphans cleaned",
                    qdrant_orphans,
                    falkordb_orphans,
                    falkordb_entities,
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in memory reconcile loop: %s", e)
        try:
            await sleep_fn(interval_seconds)
        except asyncio.CancelledError:
            break
        if is_shutdown_requested():
            break
