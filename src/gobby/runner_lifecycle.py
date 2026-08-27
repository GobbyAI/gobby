"""GobbyRunner daemon lifecycle compatibility and orchestration."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import nullcontext
from functools import partial
from typing import TYPE_CHECKING, Any

import uvicorn

from gobby.app_context import clear_app_context
from gobby.runner_gate import acquire_runner_gate
from gobby.runner_lifecycle_agents import (
    _reconcile_agent_runs_after_restart,
    _recover_agent_runs_after_restart,
)
from gobby.runner_lifecycle_periodic import start_periodic_tasks
from gobby.runner_lifecycle_processes import _reap_remaining_child_processes
from gobby.runner_lifecycle_shutdown import (
    _await_critical_stop_hook_grace_window,
    _shutdown_websocket_server,
    shutdown_daemon_services,
)
from gobby.runner_lifecycle_startup import StartupTracker, _log_subsystem_init_result
from gobby.runner_lifecycle_subsystems import init_subsystems
from gobby.runner_pid_file import FailOpenPidOwnership, PidOwnershipResolution
from gobby.shutdown_intent import clear_active_shutdown_intent
from gobby.telemetry import shutdown_telemetry

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)

__all__ = [
    "StartupTracker",
    "_await_critical_stop_hook_grace_window",
    "_init_subsystems",
    "_log_subsystem_init_result",
    "_reap_remaining_child_processes",
    "_recover_agent_runs_after_restart",
    "_reconcile_agent_runs_after_restart",
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
        reconcile_agent_runs_after_restart=_reconcile_agent_runs_after_restart,
    )


def _start_periodic_tasks(runner: GobbyRunner, **loops: Any) -> None:
    """Compatibility wrapper for periodic background task startup."""
    start_periodic_tasks(runner, tracker=_startup_tracker, **loops)


async def _start_web_chat_runtime(runner: GobbyRunner) -> None:
    """Start daemon-owned chat subprocesses after the HTTP socket is ready."""
    runtime_manager = getattr(runner.http_server.services, "web_chat_runtime_manager", None)
    if runtime_manager is None:
        return
    try:
        await runtime_manager.start(background=True)
        logger.debug("Web chat runtime manager startup scheduled")
    except Exception as e:
        logger.warning("Failed to start web chat runtime manager: %s", e)


async def _serve_http(server: uvicorn.Server) -> BaseException | None:
    """Run uvicorn and return failures so its task cannot terminate the event loop."""
    try:
        await server.serve()
    except BaseException as exc:
        return exc
    return None


async def run_daemon(
    runner: GobbyRunner,
    *,
    ownership_resolution: PidOwnershipResolution,
) -> None:
    """Main daemon startup, event loop, and shutdown sequence.

    ``ownership_resolution`` proves singleton ownership was resolved before construction.
    """
    bootstrap_config = runner.bootstrap_config
    from gobby.runner_maintenance import (
        bin_freshness_loop,
        cleanup_chat_attachments_loop,
        cleanup_comms_messages_loop,
        cleanup_expired_isolation_loop,
        cleanup_pid_file,
        cleanup_zombie_messages_loop,
        drain_hook_inbox_loop,
        expire_approval_timeouts_loop,
        loop_progress_cleanup_loop,
        memory_reconcile_loop,
        metric_snapshot_loop,
        metrics_archive_loop,
        metrics_cleanup_loop,
        purge_deleted_skills_loop,
        rebuild_vector_store,
        setup_signal_handlers,
        span_cleanup_loop,
        tmux_window_name_repair_loop,
        unmodeled_observation_cleanup_loop,
    )

    def cleanup_owned_pid_file() -> None:
        try:
            cleanup_pid_file()
        finally:
            ownership_resolution.release()

    try:
        global _startup_tracker
        _startup_tracker = StartupTracker()

        main_loop = asyncio.get_running_loop()
        runner.main_loop = main_loop
        http_services = getattr(runner.http_server, "services", None)
        if http_services is not None:
            http_services.main_loop = main_loop

        setup_signal_handlers(
            runner.request_shutdown,
            shutdown_intent_callback=runner.request_shutdown,
        )

        if isinstance(ownership_resolution, FailOpenPidOwnership):
            logger.warning(
                "Running without PID-file ownership after advisory lock failure: %s",
                ownership_resolution.error,
            )

        database = getattr(runner, "database", None)
        conninfo = getattr(database, "conninfo", None)
        if isinstance(conninfo, str) and conninfo:
            try:
                application_name = getattr(database, "application_name", None)
                if not isinstance(application_name, str) or not application_name:
                    raise RuntimeError(
                        "PostgreSQL runner gate requires a lifecycle application name"
                    )
                await acquire_runner_gate(
                    conninfo,
                    successor_application_name=application_name,
                )
            except BaseException as gate_error:
                from gobby.runner_rollback import rollback_runner_resources

                rollback_runner_resources(runner)
                cleanup_owned_pid_file()
                if isinstance(gate_error, asyncio.CancelledError):
                    raise
                raise SystemExit(1) from gate_error

        from gobby.agents.terminal_delivery import (
            configure_terminal_delivery_offload,
            reopen_terminal_delivery_admission,
        )

        reopen_terminal_delivery_admission()
        configure_terminal_delivery_offload(
            async_offload=runner.db_executor.run,
            sync_submit=runner.db_executor.submit,
        )

        from gobby.runner_service_readiness import require_managed_services_ready

        await require_managed_services_ready(runner)

        uvicorn_drain_timeout = 15
        config = uvicorn.Config(
            runner.http_server.app,
            host=bootstrap_config.bind_host,
            port=runner.http_server.port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=uvicorn_drain_timeout,
        )
        server = uvicorn.Server(config)
        # Gobby owns process signals so lifecycle events can finish before
        # Uvicorn tears down the FastAPI lifespan and workflow runtime.
        object.__setattr__(server, "capture_signals", nullcontext)
        from gobby.servers.uvicorn_shutdown import (
            install_uvicorn_shutdown_filter,
            remove_uvicorn_shutdown_filter,
        )

        shutdown_log_filter = install_uvicorn_shutdown_filter(
            lambda: bool(getattr(runner.http_server.services, "shutdown_in_progress", False))
        )
        try:
            server_task = asyncio.create_task(_serve_http(server))

            unexpected_server_exit = asyncio.Event()

            def observe_server_exit(task: asyncio.Future[BaseException | None]) -> None:
                if runner._shutdown_requested:
                    return
                unexpected_server_exit.set()
                failure_context = (
                    "HTTP server exited unexpectedly"
                    if server.started
                    else "HTTP server failed before binding"
                )
                logger.error(
                    "%s (%r); requesting daemon shutdown",
                    failure_context,
                    task.result(),
                )
                runner._shutdown_requested = True

            server_task.add_done_callback(observe_server_exit)

            while not server.started and not server_task.done() and not runner._shutdown_requested:
                await asyncio.sleep(0.01)

            if server.started and not runner._shutdown_requested:
                clear_active_shutdown_intent()
                await _start_web_chat_runtime(runner)
                runner._subsystem_init_task = asyncio.create_task(
                    _init_subsystems(runner, rebuild_vector_store),
                    name="subsystem-init",
                )
                runner._subsystem_init_task.add_done_callback(
                    partial(_log_subsystem_init_result, tracker=_startup_tracker)
                )

                _start_periodic_tasks(
                    runner,
                    metrics_cleanup_loop=metrics_cleanup_loop,
                    metrics_archive_loop=metrics_archive_loop,
                    span_cleanup_loop=span_cleanup_loop,
                    unmodeled_observation_cleanup_loop=unmodeled_observation_cleanup_loop,
                    loop_progress_cleanup_loop=loop_progress_cleanup_loop,
                    memory_reconcile_loop=memory_reconcile_loop,
                    cleanup_zombie_messages_loop=cleanup_zombie_messages_loop,
                    cleanup_comms_messages_loop=cleanup_comms_messages_loop,
                    purge_deleted_skills_loop=purge_deleted_skills_loop,
                    cleanup_chat_attachments_loop=cleanup_chat_attachments_loop,
                    cleanup_expired_isolation_loop=cleanup_expired_isolation_loop,
                    metric_snapshot_loop=metric_snapshot_loop,
                    bin_freshness_loop=bin_freshness_loop,
                    drain_hook_inbox_loop=drain_hook_inbox_loop,
                    expire_approval_timeouts_loop=expire_approval_timeouts_loop,
                    tmux_window_name_repair_loop=tmux_window_name_repair_loop,
                )

            while not runner._shutdown_requested:
                await asyncio.sleep(0.5)

            server_failure = server_task.result() if unexpected_server_exit.is_set() else None

            await shutdown_daemon_services(
                runner,
                server,
                server_task,
                uvicorn_drain_timeout,
                await_critical_stop_hook_grace_window=_await_critical_stop_hook_grace_window,
                shutdown_websocket_server=_shutdown_websocket_server,
                reap_remaining_child_processes=_reap_remaining_child_processes,
                shutdown_telemetry=shutdown_telemetry,
                cleanup_pid_file=cleanup_owned_pid_file,
            )
            if unexpected_server_exit.is_set():
                from gobby.runner import _healthy_daemon_running

                if _healthy_daemon_running(runner.http_server.port, bootstrap_config.bind_host):
                    logger.info("Lost the HTTP bind race to a healthy daemon; exiting cleanly")
                    return
                logger.error("HTTP server exited unexpectedly: %s", server_failure)
                if server_failure is None:
                    raise SystemExit(1)
                raise SystemExit(1) from server_failure
        finally:
            remove_uvicorn_shutdown_filter(shutdown_log_filter)

    except Exception as e:
        logger.exception("Fatal error: %s", e)
        from gobby.runner_rollback import rollback_runner_resources

        rollback_runner_resources(runner)
        cleanup_owned_pid_file()
        sys.exit(1)
    finally:
        clear_app_context()
