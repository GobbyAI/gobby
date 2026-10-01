"""Periodic maintenance task startup for the daemon lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from gobby.runner_lifecycle_startup import StartupTracker

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner


logger = logging.getLogger(__name__)

_RUNTIME_OUTPUT_OVER_LIMIT_SERVICE = "runtime_output_over_limit"

# Periodic tasks that sweep or write shared hub rows; a `node` runner skips them.
HUB_ONLY_PERIODIC_TASKS = frozenset(
    {
        "metrics-cleanup",
        "test-schema-sweep",
        "tool-result-cleanup",
        "workflow-audit-cleanup",
        "metrics-archive",
        "model-metadata-refresh",
        "provider-capability-refresh",
        "span-cleanup",
        "unmodeled-observation-cleanup",
        "loop-progress-cleanup",
        "memory-reconcile",
        "zombie-message-cleanup",
        "comms-message-cleanup",
        "skill-retention-purge",
        "chat-attachment-cleanup",
        "hook-receipt-retention",
        "approval-timeout-expiry",
        "metric-snapshot",
    }
)


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
    node_mode = runner.bootstrap_config.run_mode() == "node"

    def hub_only_skipped(name: str) -> bool:
        if node_mode and name in HUB_ONLY_PERIODIC_TASKS:
            logger.info("skipping hub-only %s in node mode", name)
            return True
        return False

    def spawn(
        name: str, start: Callable[[], Coroutine[Any, Any, None]]
    ) -> asyncio.Task[None] | None:
        if hub_only_skipped(name):
            return None
        return asyncio.create_task(start(), name=name)

    config = runner.config_runtime.capture().snapshot.active
    loops = {**_default_loops(), **loops}
    db_executor = getattr(runner, "db_executor", None)
    memory_manager = getattr(runner, "memory_manager", None)
    runner._metrics_cleanup_task = spawn(
        "metrics-cleanup",
        lambda: loops["metrics_cleanup_loop"](
            runner.metrics_manager,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
    )
    runner._test_schema_sweep_task = spawn(
        "test-schema-sweep",
        lambda: loops["sweep_test_schemas_loop"](
            config.database_url,
            lambda: runner._shutdown_requested,
            capture_database_url=lambda: (
                runner.config_runtime.capture().snapshot.active.database_url
            ),
        ),
    )
    runner._tool_results_cleanup_task = spawn(
        "tool-result-cleanup",
        lambda: loops["tool_result_cleanup_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
            run_db=getattr(db_executor, "run", None),
        ),
    )
    runner._workflow_audit_cleanup_task = spawn(
        "workflow-audit-cleanup",
        lambda: loops["workflow_audit_cleanup_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
            run_db=getattr(db_executor, "run", None),
        ),
    )
    runner._metrics_archive_task = spawn(
        "metrics-archive",
        lambda: loops["metrics_archive_loop"](
            runner.metrics_event_store,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
    )
    services = getattr(getattr(runner, "http_server", None), "services", None)
    model_metadata_coverage_auditor = getattr(
        services,
        "model_metadata_coverage_auditor",
        None,
    )
    runner._model_metadata_refresh_task = spawn(
        "model-metadata-refresh",
        lambda: loops["model_metadata_refresh_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            coverage_auditor=model_metadata_coverage_auditor,
        ),
    )
    runner._provider_capability_refresh_task = None
    provider_capability_service = getattr(services, "provider_capability_service", None)
    if provider_capability_service is not None and not hub_only_skipped(
        "provider-capability-refresh"
    ):
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

    runner._span_cleanup_task = spawn(
        "span-cleanup",
        lambda: loops["span_cleanup_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
        ),
    )
    runner._unmodeled_observations_cleanup_task = spawn(
        "unmodeled-observation-cleanup",
        lambda: loops["unmodeled_observation_cleanup_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
    )
    runner._loop_progress_cleanup_task = spawn(
        "loop-progress-cleanup",
        lambda: loops["loop_progress_cleanup_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
    )

    runner._memory_reconcile_task = None
    if memory_manager:
        runner._memory_reconcile_task = spawn(
            "memory-reconcile",
            lambda: loops["memory_reconcile_loop"](
                memory_manager, lambda: runner._shutdown_requested
            ),
        )

    runner._zombie_messages_task = spawn(
        "zombie-message-cleanup",
        lambda: loops["cleanup_zombie_messages_loop"](
            runner.database, lambda: runner._shutdown_requested
        ),
    )
    runner._comms_messages_task = spawn(
        "comms-message-cleanup",
        lambda: loops["cleanup_comms_messages_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
    )
    runner._skill_purge_task = spawn(
        "skill-retention-purge",
        lambda: loops["purge_deleted_skills_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
            run_db=getattr(db_executor, "run", None),
        ),
    )
    runner._chat_attachments_cleanup_task = spawn(
        "chat-attachment-cleanup",
        lambda: loops["cleanup_chat_attachments_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
            run_db=getattr(db_executor, "run", None),
        ),
    )
    runner._expired_isolation_task = spawn(
        "expired-isolation-cleanup",
        lambda: loops["cleanup_expired_isolation_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
            worktree_delete_executor=getattr(runner, "worktree_delete_executor", None),
        ),
    )
    runner._metric_snapshot_task = spawn(
        "metric-snapshot",
        lambda: loops["metric_snapshot_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
    )

    def set_runtime_output_over_limit(over_limit: bool) -> None:
        if over_limit:
            runner.degraded_services.add(_RUNTIME_OUTPUT_OVER_LIMIT_SERVICE)
        else:
            runner.degraded_services.discard(_RUNTIME_OUTPUT_OVER_LIMIT_SERVICE)

    runner._resource_monitor_task = spawn(
        "resource-monitor",
        lambda: loops["resource_monitor_loop"](
            lambda: runner._shutdown_requested,
            set_runtime_output_over_limit,
            capture_bundle=runner.config_runtime.capture,
        ),
    )
    runner._hook_inbox_task = spawn(
        "hook-inbox-drain",
        lambda: loops["drain_hook_inbox_loop"](
            runner.http_server.app, lambda: runner._shutdown_requested
        ),
    )
    runner._hook_quarantine_retention_task = spawn(
        "hook-quarantine-retention",
        lambda: loops["hook_quarantine_retention_loop"](lambda: runner._shutdown_requested),
    )
    runner._hook_receipt_retention_task = spawn(
        "hook-receipt-retention",
        lambda: loops["hook_receipt_retention_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
    )
    runner._bin_freshness_task = spawn(
        "bin-freshness",
        lambda: loops["bin_freshness_loop"](
            runner.database,
            lambda: runner._shutdown_requested,
            capture_bundle=runner.config_runtime.capture,
            run_db=getattr(db_executor, "run", None),
        ),
    )

    from gobby.storage.pipelines import LocalPipelineExecutionManager

    runner._approval_timeout_task = spawn(
        "approval-timeout-expiry",
        lambda: loops["expire_approval_timeouts_loop"](
            LocalPipelineExecutionManager(runner.database, project_id=None),
            lambda: runner._shutdown_requested,
            run_db=getattr(db_executor, "run", None),
        ),
    )

    runner._tmux_window_repair_task = spawn(
        "tmux-window-repair",
        lambda: loops["tmux_window_name_repair_loop"](
            getattr(runner, "session_manager", None),
            lambda: runner._shutdown_requested,
            startup_ready=lambda: runner.http_server.services.startup_ready,
        ),
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
