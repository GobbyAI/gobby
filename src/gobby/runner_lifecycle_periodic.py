"""Periodic maintenance task startup for the daemon lifecycle."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.runner_lifecycle_startup import StartupTracker

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner


def start_periodic_tasks(
    runner: GobbyRunner,
    *,
    tracker: StartupTracker | None,
    **loops: Any,
) -> None:
    """Start all lightweight periodic background tasks."""
    runner._metrics_cleanup_task = asyncio.create_task(
        loops["metrics_cleanup_loop"](runner.metrics_manager, lambda: runner._shutdown_requested),
        name="metrics-cleanup",
    )
    runner._metrics_archive_task = asyncio.create_task(
        loops["metrics_archive_loop"](
            runner.metrics_event_store, lambda: runner._shutdown_requested
        ),
        name="metrics-archive",
    )

    retention_days = 7
    if runner.config.telemetry and hasattr(runner.config.telemetry, "trace_retention_days"):
        retention_days = runner.config.telemetry.trace_retention_days
    runner._span_cleanup_task = asyncio.create_task(
        loops["span_cleanup_loop"](
            runner.database, lambda: runner._shutdown_requested, retention_days=retention_days
        ),
        name="span-cleanup",
    )

    runner._memory_reconcile_task = None
    if runner.memory_manager:
        runner._memory_reconcile_task = asyncio.create_task(
            loops["memory_reconcile_loop"](
                runner.memory_manager, lambda: runner._shutdown_requested
            ),
            name="memory-reconcile",
        )

    runner._zombie_messages_task = asyncio.create_task(
        loops["cleanup_zombie_messages_loop"](runner.database, lambda: runner._shutdown_requested),
        name="zombie-message-cleanup",
    )
    runner._comms_messages_task = asyncio.create_task(
        loops["cleanup_comms_messages_loop"](runner.database, lambda: runner._shutdown_requested),
        name="comms-message-cleanup",
    )
    runner._expired_isolation_task = asyncio.create_task(
        loops["cleanup_expired_isolation_loop"](
            runner.database, lambda: runner._shutdown_requested
        ),
        name="expired-isolation-cleanup",
    )
    runner._metric_snapshot_task = asyncio.create_task(
        loops["metric_snapshot_loop"](runner.database, lambda: runner._shutdown_requested),
        name="metric-snapshot",
    )
    runner._hook_inbox_task = asyncio.create_task(
        loops["drain_hook_inbox_loop"](runner.http_server.app, lambda: runner._shutdown_requested),
        name="hook-inbox-drain",
    )
    runner._bin_freshness_task = None
    bin_freshness_config = getattr(runner.config, "bin_freshness", None)
    if isinstance(bin_freshness_config, BinFreshnessConfig) and bin_freshness_config.enabled:
        runner._bin_freshness_task = asyncio.create_task(
            loops["bin_freshness_loop"](
                runner.database,
                bin_freshness_config,
                lambda: runner._shutdown_requested,
            ),
            name="bin-freshness",
        )

    runner._approval_timeout_task = None
    if runner.pipeline_execution_manager:
        runner._approval_timeout_task = asyncio.create_task(
            loops["expire_approval_timeouts_loop"](
                runner.pipeline_execution_manager, lambda: runner._shutdown_requested
            ),
            name="approval-timeout-expiry",
        )

    task_count = sum(
        1
        for task in (
            runner._metrics_cleanup_task,
            runner._metrics_archive_task,
            runner._span_cleanup_task,
            getattr(runner, "_memory_reconcile_task", None),
            runner._zombie_messages_task,
            runner._comms_messages_task,
            runner._expired_isolation_task,
            runner._metric_snapshot_task,
            runner._hook_inbox_task,
            runner._bin_freshness_task,
            runner._approval_timeout_task,
        )
        if task is not None
    )
    if tracker:
        tracker.schedule(f"Periodic maintenance ({task_count} tasks)")
