"""Periodic maintenance task startup for the daemon lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, Any

from gobby.runner_lifecycle_startup import StartupTracker

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner


logger = logging.getLogger(__name__)

_RUNTIME_OUTPUT_OVER_LIMIT_SERVICE = "runtime_output_over_limit"


def _log_periodic_task_failure(task: asyncio.Task[None]) -> None:
    """Log periodic task failures so a dead maintenance job is visible in daemon logs."""
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        logger.error(
            "Periodic task %s failed",
            task.get_name(),
            exc_info=(type(error), error, error.__traceback__),
        )


def _default_loops() -> dict[str, Any]:
    from gobby.runner_maintenance import (
        bin_freshness_loop,
        cleanup_chat_attachments_loop,
        cleanup_comms_messages_loop,
        cleanup_expired_isolation_loop,
        cleanup_zombie_messages_loop,
        drain_hook_inbox_loop,
        expire_approval_timeouts_loop,
        hook_quarantine_retention_loop,
        hook_receipt_retention_loop,
        loop_progress_cleanup_loop,
        memory_reconcile_loop,
        metric_snapshot_loop,
        metrics_archive_loop,
        metrics_cleanup_loop,
        purge_deleted_skills_loop,
        recall_drift_monitor_loop,
        span_cleanup_loop,
        sweep_test_schemas_loop,
        tmux_window_name_repair_loop,
        unmodeled_observation_cleanup_loop,
    )
    from gobby.runner_maintenance_audit import workflow_audit_cleanup_loop
    from gobby.runner_maintenance_recurring import tool_result_cleanup_loop
    from gobby.runner_maintenance_resources import resource_monitor_loop
    from gobby.runner_model_metadata_refresh import model_metadata_refresh_loop

    return {
        "metrics_cleanup_loop": metrics_cleanup_loop,
        "tool_result_cleanup_loop": tool_result_cleanup_loop,
        "workflow_audit_cleanup_loop": workflow_audit_cleanup_loop,
        "metrics_archive_loop": metrics_archive_loop,
        "span_cleanup_loop": span_cleanup_loop,
        "sweep_test_schemas_loop": sweep_test_schemas_loop,
        "unmodeled_observation_cleanup_loop": unmodeled_observation_cleanup_loop,
        "memory_reconcile_loop": memory_reconcile_loop,
        "cleanup_zombie_messages_loop": cleanup_zombie_messages_loop,
        "cleanup_comms_messages_loop": cleanup_comms_messages_loop,
        "purge_deleted_skills_loop": purge_deleted_skills_loop,
        "cleanup_chat_attachments_loop": cleanup_chat_attachments_loop,
        "cleanup_expired_isolation_loop": cleanup_expired_isolation_loop,
        "metric_snapshot_loop": metric_snapshot_loop,
        "recall_drift_monitor_loop": recall_drift_monitor_loop,
        "bin_freshness_loop": bin_freshness_loop,
        "drain_hook_inbox_loop": drain_hook_inbox_loop,
        "hook_quarantine_retention_loop": hook_quarantine_retention_loop,
        "hook_receipt_retention_loop": hook_receipt_retention_loop,
        "expire_approval_timeouts_loop": expire_approval_timeouts_loop,
        "loop_progress_cleanup_loop": loop_progress_cleanup_loop,
        "tmux_window_name_repair_loop": tmux_window_name_repair_loop,
        "resource_monitor_loop": resource_monitor_loop,
        "model_metadata_refresh_loop": model_metadata_refresh_loop,
    }


def start_periodic_tasks(
    runner: GobbyRunner,
    *,
    tracker: StartupTracker | None,
    **loops: Any,
) -> None:
    """Start all lightweight periodic background tasks."""
    config = runner.config_runtime.capture().snapshot.active
    loops = {**_default_loops(), **loops}
    db_executor = getattr(runner, "db_executor", None)
    memory_manager = getattr(runner, "memory_manager", None)
    runner._metrics_cleanup_task = asyncio.create_task(
        loops["metrics_cleanup_loop"](
            runner.metrics_manager,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
        name="metrics-cleanup",
    )
    runner._test_schema_sweep_task = asyncio.create_task(
        loops["sweep_test_schemas_loop"](
            config.database_url,
            lambda: runner._shutdown_requested,
            capture_database_url=lambda: (
                runner.config_runtime.capture().snapshot.active.database_url
            ),
        ),
        name="test-schema-sweep",
    )
    runner._tool_results_cleanup_task = asyncio.create_task(
        loops["tool_result_cleanup_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
            run_db=getattr(db_executor, "run", None),
        ),
        name="tool-result-cleanup",
    )
    runner._workflow_audit_cleanup_task = asyncio.create_task(
        loops["workflow_audit_cleanup_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
            run_db=getattr(db_executor, "run", None),
        ),
        name="workflow-audit-cleanup",
    )
    runner._metrics_archive_task = asyncio.create_task(
        loops["metrics_archive_loop"](
            runner.metrics_event_store,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
        name="metrics-archive",
    )
    services = getattr(getattr(runner, "http_server", None), "services", None)
    model_metadata_coverage_auditor = getattr(
        services,
        "model_metadata_coverage_auditor",
        None,
    )
    runner._model_metadata_refresh_task = asyncio.create_task(
        loops["model_metadata_refresh_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            coverage_auditor=model_metadata_coverage_auditor,
        ),
        name="model-metadata-refresh",
    )
    runner._provider_capability_refresh_task = None
    provider_capability_service = getattr(services, "provider_capability_service", None)
    if provider_capability_service is not None:
        run = getattr(provider_capability_service, "run", None)
        refresh_loop = run(lambda: runner._shutdown_requested) if callable(run) else None
        if inspect.iscoroutine(refresh_loop):
            runner._provider_capability_refresh_task = asyncio.create_task(
                refresh_loop,
                name="provider-capability-refresh",
            )

    runner._generation_endpoint_health_task = None
    generation_endpoint_health = getattr(services, "generation_endpoint_health", None)
    if generation_endpoint_health is not None:
        run = getattr(generation_endpoint_health, "run", None)
        refresh_loop = run(lambda: runner._shutdown_requested) if callable(run) else None
        if inspect.iscoroutine(refresh_loop):
            runner._generation_endpoint_health_task = asyncio.create_task(
                refresh_loop,
                name="generation-endpoint-health",
            )

    runner._span_cleanup_task = asyncio.create_task(
        loops["span_cleanup_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
        ),
        name="span-cleanup",
    )
    runner._unmodeled_observations_cleanup_task = asyncio.create_task(
        loops["unmodeled_observation_cleanup_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
        name="unmodeled-observation-cleanup",
    )
    runner._loop_progress_cleanup_task = asyncio.create_task(
        loops["loop_progress_cleanup_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
        name="loop-progress-cleanup",
    )

    runner._memory_reconcile_task = None
    if memory_manager:
        runner._memory_reconcile_task = asyncio.create_task(
            loops["memory_reconcile_loop"](memory_manager, lambda: runner._shutdown_requested),
            name="memory-reconcile",
        )

    runner._recall_drift_task = None
    if memory_manager:
        runner._recall_drift_task = asyncio.create_task(
            loops["recall_drift_monitor_loop"](
                runner.database,
                lambda: runner._shutdown_requested,
                capture_bundle=runner.config_runtime.capture,
            ),
            name="recall-drift-monitor",
        )

    runner._zombie_messages_task = asyncio.create_task(
        loops["cleanup_zombie_messages_loop"](runner.database, lambda: runner._shutdown_requested),
        name="zombie-message-cleanup",
    )
    runner._comms_messages_task = asyncio.create_task(
        loops["cleanup_comms_messages_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
        name="comms-message-cleanup",
    )
    runner._skill_purge_task = asyncio.create_task(
        loops["purge_deleted_skills_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
            run_db=getattr(db_executor, "run", None),
        ),
        name="skill-retention-purge",
    )
    runner._chat_attachments_cleanup_task = asyncio.create_task(
        loops["cleanup_chat_attachments_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
            run_db=getattr(db_executor, "run", None),
        ),
        name="chat-attachment-cleanup",
    )
    runner._expired_isolation_task = asyncio.create_task(
        loops["cleanup_expired_isolation_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
            worktree_delete_executor=getattr(runner, "worktree_delete_executor", None),
        ),
        name="expired-isolation-cleanup",
    )
    runner._metric_snapshot_task = asyncio.create_task(
        loops["metric_snapshot_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
        name="metric-snapshot",
    )

    def set_runtime_output_over_limit(over_limit: bool) -> None:
        if over_limit:
            runner.degraded_services.add(_RUNTIME_OUTPUT_OVER_LIMIT_SERVICE)
        else:
            runner.degraded_services.discard(_RUNTIME_OUTPUT_OVER_LIMIT_SERVICE)

    runner._resource_monitor_task = asyncio.create_task(
        loops["resource_monitor_loop"](
            lambda: runner._shutdown_requested,
            set_runtime_output_over_limit,
            capture_bundle=runner.config_runtime.capture,
        ),
        name="resource-monitor",
    )
    runner._hook_inbox_task = asyncio.create_task(
        loops["drain_hook_inbox_loop"](runner.http_server.app, lambda: runner._shutdown_requested),
        name="hook-inbox-drain",
    )
    runner._hook_quarantine_retention_task = asyncio.create_task(
        loops["hook_quarantine_retention_loop"](lambda: runner._shutdown_requested),
        name="hook-quarantine-retention",
    )
    runner._hook_receipt_retention_task = asyncio.create_task(
        loops["hook_receipt_retention_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
        name="hook-receipt-retention",
    )
    runner._bin_freshness_task = asyncio.create_task(
        loops["bin_freshness_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
            run_db=getattr(db_executor, "run", None),
        ),
        name="bin-freshness",
    )

    runner._approval_timeout_task = None
    from gobby.storage.pipelines import LocalPipelineExecutionManager

    approval_timeout_manager = LocalPipelineExecutionManager(runner.database, project_id=None)
    runner._approval_timeout_task = asyncio.create_task(
        loops["expire_approval_timeouts_loop"](
            approval_timeout_manager,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
        name="approval-timeout-expiry",
    )

    runner._tmux_window_repair_task = asyncio.create_task(
        loops["tmux_window_name_repair_loop"](
            getattr(runner, "session_manager", None),
            lambda: runner._shutdown_requested,
            startup_ready=lambda: runner.http_server.services.startup_ready,
        ),
        name="tmux-window-repair",
    )

    periodic_tasks = tuple(
        task
        for task in (
            runner._metrics_cleanup_task,
            runner._test_schema_sweep_task,
            runner._tool_results_cleanup_task,
            runner._workflow_audit_cleanup_task,
            runner._metrics_archive_task,
            runner._model_metadata_refresh_task,
            runner._provider_capability_refresh_task,
            runner._generation_endpoint_health_task,
            runner._span_cleanup_task,
            runner._unmodeled_observations_cleanup_task,
            runner._loop_progress_cleanup_task,
            getattr(runner, "_memory_reconcile_task", None),
            getattr(runner, "_recall_drift_task", None),
            runner._zombie_messages_task,
            runner._comms_messages_task,
            runner._skill_purge_task,
            runner._chat_attachments_cleanup_task,
            runner._expired_isolation_task,
            runner._metric_snapshot_task,
            runner._resource_monitor_task,
            runner._hook_inbox_task,
            runner._hook_quarantine_retention_task,
            runner._hook_receipt_retention_task,
            runner._bin_freshness_task,
            runner._approval_timeout_task,
            runner._tmux_window_repair_task,
        )
        if task is not None
    )
    for task in periodic_tasks:
        task.add_done_callback(_log_periodic_task_failure)

    if tracker:
        tracker.schedule(f"Periodic maintenance ({len(periodic_tasks)} tasks)")
