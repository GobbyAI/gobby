"""GobbyRunner daemon lifecycle compatibility and orchestration."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

import uvicorn

from gobby.runner_lifecycle_agents import (
    _cancel_active_agent_runs_for_shutdown,
    _cleanup_persisted_completion_subscribers,
    _reconcile_agent_runs_after_restart,
    _recover_agent_runs_after_restart,
    _register_persisted_completion_subscribers,
    _replay_daemon_restart_agent_cancellations,
)
from gobby.runner_lifecycle_periodic import start_periodic_tasks
from gobby.runner_lifecycle_shutdown import (
    _await_critical_stop_hook_grace_window,
    _reap_remaining_child_processes,
    _shutdown_websocket_server,
    shutdown_daemon_services,
)
from gobby.runner_lifecycle_startup import (
    StartupTracker,
    _log_subsystem_init_result,
    _record_provider_model_refresh_result,
    _refresh_provider_model_catalog,
)
from gobby.runner_lifecycle_subsystems import init_subsystems
from gobby.telemetry import shutdown_telemetry

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)

__all__ = [
    "StartupTracker",
    "_await_critical_stop_hook_grace_window",
    "_cancel_active_agent_runs_for_shutdown",
    "_cleanup_persisted_completion_subscribers",
    "_init_subsystems",
    "_log_subsystem_init_result",
    "_reap_remaining_child_processes",
    "_recover_agent_runs_after_restart",
    "_reconcile_agent_runs_after_restart",
    "_record_provider_model_refresh_result",
    "_refresh_provider_model_catalog",
    "_register_persisted_completion_subscribers",
    "_replay_daemon_restart_agent_cancellations",
    "_shutdown_websocket_server",
    "_start_periodic_tasks",
    "get_startup_tracker",
    "run_daemon",
    "shutdown_telemetry",
]

# Module-level state is kept here so existing admin imports and tests that
# patch gobby.runner_lifecycle._startup_tracker continue to affect runtime.
_startup_tracker: StartupTracker | None = None


def get_startup_tracker() -> StartupTracker | None:
    """Return the current startup tracker (used by admin API)."""
    return _startup_tracker


async def _init_subsystems(runner: GobbyRunner, rebuild_vector_store: Any) -> None:
    """Compatibility wrapper for progressive subsystem initialization."""
    await init_subsystems(
        runner,
        rebuild_vector_store,
        _startup_tracker,
        refresh_provider_model_catalog=_refresh_provider_model_catalog,
        record_provider_model_refresh_result=_record_provider_model_refresh_result,
        reconcile_agent_runs_after_restart=_reconcile_agent_runs_after_restart,
    )


def _start_periodic_tasks(runner: GobbyRunner, **loops: Any) -> None:
    """Compatibility wrapper for periodic background task startup."""
    start_periodic_tasks(runner, tracker=_startup_tracker, **loops)


async def run_daemon(runner: GobbyRunner) -> None:
    """Main daemon startup, event loop, and shutdown sequence."""
    from gobby.runner_maintenance import (
        bin_freshness_loop,
        cleanup_chat_attachments_loop,
        cleanup_comms_messages_loop,
        cleanup_expired_isolation_loop,
        cleanup_pid_file,
        cleanup_zombie_messages_loop,
        drain_hook_inbox_loop,
        expire_approval_timeouts_loop,
        memory_reconcile_loop,
        metric_snapshot_loop,
        metrics_archive_loop,
        metrics_cleanup_loop,
        rebuild_vector_store,
        setup_signal_handlers,
        span_cleanup_loop,
    )

    try:
        global _startup_tracker
        _startup_tracker = StartupTracker()

        setup_signal_handlers(
            lambda: setattr(runner, "_shutdown_requested", True),
            shutdown_intent_callback=lambda intent: setattr(runner, "_shutdown_intent", intent),
        )

        from gobby.cli.utils import get_gobby_home
        from gobby.config.mcp import migrate_legacy_mcp_config

        pid_file = get_gobby_home() / "gobby.pid"
        try:
            pid_file.write_text(str(os.getpid()))
            logger.info(f"Wrote PID file: {pid_file} (PID {os.getpid()})")
        except OSError as e:
            logger.warning(f"Could not write PID file {pid_file}: {e}")

        try:
            migrate_legacy_mcp_config()
        except OSError as e:
            logger.warning(f"Failed to migrate legacy MCP config: {e}")

        uvicorn_drain_timeout = 15
        config = uvicorn.Config(
            runner.http_server.app,
            host=runner.config.bind_host,
            port=runner.http_server.port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=uvicorn_drain_timeout,
        )
        server = uvicorn.Server(config)
        server_task = asyncio.create_task(server.serve())

        runner._subsystem_init_task = asyncio.create_task(
            _init_subsystems(runner, rebuild_vector_store),
            name="subsystem-init",
        )
        runner._subsystem_init_task.add_done_callback(_log_subsystem_init_result)

        _start_periodic_tasks(
            runner,
            metrics_cleanup_loop=metrics_cleanup_loop,
            metrics_archive_loop=metrics_archive_loop,
            span_cleanup_loop=span_cleanup_loop,
            memory_reconcile_loop=memory_reconcile_loop,
            cleanup_zombie_messages_loop=cleanup_zombie_messages_loop,
            cleanup_comms_messages_loop=cleanup_comms_messages_loop,
            cleanup_chat_attachments_loop=cleanup_chat_attachments_loop,
            cleanup_expired_isolation_loop=cleanup_expired_isolation_loop,
            metric_snapshot_loop=metric_snapshot_loop,
            bin_freshness_loop=bin_freshness_loop,
            drain_hook_inbox_loop=drain_hook_inbox_loop,
            expire_approval_timeouts_loop=expire_approval_timeouts_loop,
        )

        while not runner._shutdown_requested:
            await asyncio.sleep(0.5)

        await shutdown_daemon_services(
            runner,
            server,
            server_task,
            uvicorn_drain_timeout,
            await_critical_stop_hook_grace_window=_await_critical_stop_hook_grace_window,
            shutdown_websocket_server=_shutdown_websocket_server,
            cancel_active_agent_runs_for_shutdown=_cancel_active_agent_runs_for_shutdown,
            reap_remaining_child_processes=_reap_remaining_child_processes,
            shutdown_telemetry=shutdown_telemetry,
            cleanup_pid_file=cleanup_pid_file,
        )

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        cleanup_pid_file()
        sys.exit(1)
