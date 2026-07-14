"""GobbyRunner daemon lifecycle compatibility and orchestration."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from functools import partial
from typing import TYPE_CHECKING, Any

import uvicorn

from gobby.app_context import clear_app_context
from gobby.runner_lifecycle_agents import (
    _cancel_active_agent_runs_for_shutdown,
    _reconcile_agent_runs_after_restart,
    _recover_agent_runs_after_restart,
    _register_persisted_completion_subscribers,
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
from gobby.runner_pid_file import PidFileClaim, claim_pid_file
from gobby.telemetry import shutdown_telemetry

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)

__all__ = [
    "StartupTracker",
    "_await_critical_stop_hook_grace_window",
    "_cancel_active_agent_runs_for_shutdown",
    "_init_subsystems",
    "_log_subsystem_init_result",
    "_reap_remaining_child_processes",
    "_recover_agent_runs_after_restart",
    "_reconcile_agent_runs_after_restart",
    "_record_provider_model_refresh_result",
    "_refresh_provider_model_catalog",
    "_register_persisted_completion_subscribers",
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


async def _serve_http(server: uvicorn.Server) -> BaseException | None:
    """Run uvicorn and return failures so its task cannot terminate the event loop."""
    try:
        await server.serve()
    except BaseException as exc:
        return exc
    return None


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
        tmux_window_name_repair_loop,
        unmodeled_observation_cleanup_loop,
    )

    pid_claim: PidFileClaim | None = None

    def cleanup_owned_pid_file() -> None:
        try:
            cleanup_pid_file()
        finally:
            if pid_claim is not None:
                pid_claim.release()

    try:
        global _startup_tracker
        _startup_tracker = StartupTracker()

        setup_signal_handlers(
            runner.request_shutdown,
            shutdown_intent_callback=runner.request_shutdown,
        )

        from gobby.cli.utils import get_gobby_home

        pid_file = get_gobby_home() / "gobby.pid"
        try:
            pid_claim = claim_pid_file(pid_file)
        except OSError as e:
            logger.warning(f"Could not claim PID file {pid_file}: {e}")
        if pid_claim is None:
            from gobby.runner import _healthy_daemon_running

            if _healthy_daemon_running(runner.http_server.port, runner.config.bind_host):
                logger.info(
                    "Another healthy Gobby daemon owns %s; exiting cleanly",
                    pid_file,
                )
                return
            raise RuntimeError(f"PID file is owned by another live process: {pid_file}")
        logger.info(f"Wrote PID file: {pid_file} (PID {os.getpid()})")

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
                    memory_reconcile_loop=memory_reconcile_loop,
                    cleanup_zombie_messages_loop=cleanup_zombie_messages_loop,
                    cleanup_comms_messages_loop=cleanup_comms_messages_loop,
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
                cancel_active_agent_runs_for_shutdown=_cancel_active_agent_runs_for_shutdown,
                reap_remaining_child_processes=_reap_remaining_child_processes,
                shutdown_telemetry=shutdown_telemetry,
                cleanup_pid_file=cleanup_owned_pid_file,
            )
            if unexpected_server_exit.is_set():
                from gobby.runner import _healthy_daemon_running

                if _healthy_daemon_running(runner.http_server.port, runner.config.bind_host):
                    logger.info("Lost the HTTP bind race to a healthy daemon; exiting cleanly")
                    return
                logger.error("HTTP server exited unexpectedly: %s", server_failure)
                if server_failure is None:
                    raise SystemExit(1)
                raise SystemExit(1) from server_failure
        finally:
            remove_uvicorn_shutdown_filter(shutdown_log_filter)

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        cleanup_owned_pid_file()
        sys.exit(1)
    finally:
        clear_app_context()
