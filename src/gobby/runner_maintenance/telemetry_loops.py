"""Telemetry maintenance loops."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.runner_maintenance_helpers import _run_db

if TYPE_CHECKING:
    from gobby.config.runtime import RuntimeActiveBundle

logger = logging.getLogger("gobby.runner_maintenance")
_METRIC_SNAPSHOT_CLEANUP_BATCH_LIMIT = 1000


async def span_cleanup_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    *,
    capture_bundle: Callable[[], RuntimeActiveBundle],
    interval_seconds: float = 24 * 60 * 60,
) -> None:
    """Background loop for periodic span cleanup (every 24 hours)."""
    from gobby.storage.spans import SpanStorage

    storage = SpanStorage(db)

    while not is_shutdown_requested():
        config = capture_bundle().snapshot.active
        retention_days = getattr(config.telemetry, "trace_retention_days", 7)
        try:
            deleted = storage.delete_old_spans(retention_days=retention_days)
            if deleted > 0:
                logger.info("Periodic span cleanup: removed %s old spans", deleted)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in span cleanup loop: %s", e)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


async def unmodeled_observation_cleanup_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    retention_days: int = 30,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Background loop for pruning old unmodeled-observation occurrence guards."""
    interval_seconds = 24 * 60 * 60

    from gobby.storage.unmodeled_observations import UnmodeledObservationStore

    store = UnmodeledObservationStore(db)

    while not is_shutdown_requested():
        try:
            deleted = await _run_db(
                run_db,
                store.prune_events_older_than,
                retention_days=retention_days,
            )
            if deleted > 0:
                logger.info(
                    "Periodic unmodeled-observation cleanup: removed %s old occurrence rows",
                    deleted,
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in unmodeled observation cleanup loop: %s", e)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


async def loop_progress_cleanup_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    retention_days: int = 7,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Background loop for pruning expired autonomous progress telemetry."""
    interval_seconds = 24 * 60 * 60

    from gobby.autonomous.progress_tracker import ProgressTracker

    tracker = ProgressTracker(db)

    while not is_shutdown_requested():
        try:
            deleted = await _run_db(
                run_db,
                tracker.prune_older_than,
                retention_days=retention_days,
            )
            if deleted > 0:
                logger.info(
                    "Periodic loop progress cleanup: removed %s old progress rows",
                    deleted,
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in loop progress cleanup loop: %s", e)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


async def metric_snapshot_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 60,
    retention_hours: int = 24,
    *,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Background loop that snapshots OTel metrics every interval.

    Captures get_all_metrics() output to the PostgreSQL hub for dashboard time-series charts.
    Cleans old snapshots each tick to maintain 24h retention.
    """
    from gobby.storage.metric_snapshots import MetricSnapshotStorage
    from gobby.telemetry.instruments import get_all_metrics, update_daemon_metrics

    storage = MetricSnapshotStorage(db)

    while not is_shutdown_requested():
        try:
            update_daemon_metrics()
            metrics = get_all_metrics()
            await _run_db(run_db, storage.save_snapshot, metrics)
            deleted = await _run_db(
                run_db,
                storage.delete_old_snapshots,
                retention_hours=retention_hours,
                limit=_METRIC_SNAPSHOT_CLEANUP_BATCH_LIMIT,
            )
            if deleted > 0:
                logger.debug("Metric snapshot cleanup: removed %s old snapshots", deleted)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in metric snapshot loop: %s", e)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


async def recall_drift_monitor_loop(
    db: Any,
    is_shutdown_requested: Callable[[], bool],
    *,
    capture_bundle: Callable[[], RuntimeActiveBundle],
    interval_seconds: float | None = None,
) -> None:
    """Background recall-quality drift monitor (#17201).

    Each tick replays the recent labeled recall-signal window under the
    effective constants and compares live pairwise accuracy against the
    recorded holdout baseline; ``run_drift_check_from_store`` logs a WARNING
    alarm with the rollback response path when quality regresses beyond the
    configured threshold.
    """
    from gobby.memory.recall_drift import run_drift_check_from_store
    from gobby.storage.recall_signals import RecallSignalStore

    store = RecallSignalStore(db)

    while not is_shutdown_requested():
        memory_config = capture_bundle().snapshot.active.memory
        current_interval_seconds = interval_seconds
        if current_interval_seconds is None:
            interval_hours = getattr(memory_config, "recall_drift_interval_hours", 24.0)
            current_interval_seconds = float(interval_hours) * 3600.0
        try:
            if memory_config is not None and getattr(
                memory_config, "recall_drift_monitor_enabled", False
            ):
                run_drift_check_from_store(store, memory_config)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in recall drift monitor loop: %s", e)
        try:
            await asyncio.sleep(current_interval_seconds)
        except asyncio.CancelledError:
            break
