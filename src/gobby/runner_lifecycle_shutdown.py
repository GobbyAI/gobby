"""Graceful daemon shutdown helpers."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from gobby.shutdown_intent import ShutdownIntent, coerce_shutdown_intent, get_shutdown_marker_path

if TYPE_CHECKING:
    import uvicorn

    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")

_CRITICAL_STOP_HOOK_GRACE_SECONDS = 5.0


class ReapChildProcesses(Protocol):
    async def __call__(
        self,
        *,
        preserve_agents: bool = False,
        preserved_agent_pids: set[int] | None = None,
    ) -> None: ...


def _preserved_agent_terminal_pids(runner: GobbyRunner) -> set[int]:
    """Return pane PIDs for active tmux-backed agents that survive restart."""
    agent_runner = getattr(runner, "agent_runner", None)
    run_storage = getattr(agent_runner, "run_storage", None)
    if run_storage is None:
        return set()
    try:
        runs = run_storage.list_active()
    except Exception as e:
        logger.warning("Failed to list active agent runs for restart preservation: %s", e)
        return set()
    pids: set[int] = set()
    for run in runs:
        pid = getattr(run, "pid", None)
        if getattr(run, "tmux_session_name", None) and isinstance(pid, int) and pid > 0:
            pids.add(pid)
    return pids


async def _await_critical_stop_hook_grace_window() -> None:
    """Keep HTTP available briefly so critical Stop hooks can still connect."""
    logger.debug(
        "Waiting %.1fs for critical Stop hooks before HTTP shutdown",
        _CRITICAL_STOP_HOOK_GRACE_SECONDS,
    )
    await asyncio.sleep(_CRITICAL_STOP_HOOK_GRACE_SECONDS)


async def _shutdown_websocket_server(runner: GobbyRunner, timeout: float = 5.0) -> None:
    """Stop the WebSocket server before HTTP lifespan teardown finishes."""
    websocket_task = getattr(runner, "_websocket_task", None)
    websocket_server = runner.websocket_server

    if websocket_task is not None and not websocket_task.done():
        logger.debug("Waiting for WebSocket startup task to finish before shutdown")
        try:
            await asyncio.wait_for(websocket_task, timeout=timeout)
        except TimeoutError:
            logger.warning("WebSocket startup task did not finish before shutdown; cancelling")
            websocket_task.cancel()
            try:
                await asyncio.wait_for(websocket_task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError):
                logger.warning("WebSocket startup task shutdown timed out or cancelled")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"WebSocket startup task failed during shutdown: {e}")

    if websocket_server and getattr(websocket_server, "_server", None) is not None:
        logger.debug("Stopping WebSocket server before HTTP shutdown")
        try:
            await asyncio.wait_for(websocket_server.stop(), timeout=timeout)
            logger.debug("WebSocket server stopped before HTTP shutdown")
        except TimeoutError:
            logger.warning("WebSocket server shutdown timed out")
        except Exception as e:
            logger.warning(f"WebSocket server shutdown failed: {e}")

    if websocket_task is not None:
        runner._websocket_task = None


async def _reap_remaining_child_processes(
    timeout: float = 1.0,
    *,
    preserve_agents: bool = False,
    preserved_agent_pids: set[int] | None = None,
) -> None:
    """Terminate then force-kill child processes that survived graceful shutdown."""
    try:
        import psutil

        current_process = psutil.Process(os.getpid())
        children = current_process.children(recursive=True)
        if not children:
            logger.debug("No child processes remaining after graceful shutdown")
            return

        if preserve_agents:
            preserved_pids = _expand_preserved_agent_processes(
                psutil,
                children,
                preserved_agent_pids or set(),
            )
            reapable_children = [child for child in children if child.pid not in preserved_pids]
            preserved_count = len(children) - len(reapable_children)
            if preserved_count:
                logger.info(
                    "Preserving %d terminal agent child process(es) during restart",
                    preserved_count,
                )
            children = reapable_children
            if not children:
                logger.debug("No non-agent child processes remaining after restart preservation")
                return

        logger.info(
            "Reaping %d remaining child process(es) after graceful shutdown",
            len(children),
        )
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        gone, alive = await asyncio.to_thread(psutil.wait_procs, children, timeout=timeout)
        logger.debug(
            "Child process termination sweep complete",
            extra={"terminated": len(gone), "remaining": len(alive)},
        )

        if alive:
            logger.warning(
                "Force-killing %d child process(es) still alive after graceful shutdown",
                len(alive),
            )
            for child in alive:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
    except Exception as e:
        logger.warning(f"Child process reap failed: {e}")


def _expand_preserved_agent_processes(
    psutil_module: Any,
    children: list[Any],
    root_pids: set[int],
) -> set[int]:
    """Include descendants and in-daemon ancestors for preserved agent pane PIDs."""
    preserved = set(root_pids)
    child_pids = {child.pid for child in children}
    for pid in root_pids:
        try:
            process = psutil_module.Process(pid)
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            continue
        try:
            preserved.update(child.pid for child in process.children(recursive=True))
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            pass
        try:
            parent = process.parent()
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            parent = None
        while parent is not None and parent.pid in child_pids:
            preserved.add(parent.pid)
            try:
                parent = parent.parent()
            except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
                break
    return preserved


async def _cancel_runner_task(runner: GobbyRunner, attr: str, timeout: float = 2.0) -> None:
    task = getattr(runner, attr, None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.CancelledError, TimeoutError):
            pass


async def _cancel_periodic_tasks(runner: GobbyRunner) -> None:
    for attr in (
        "_metrics_cleanup_task",
        "_metrics_archive_task",
        "_span_cleanup_task",
        "_metric_snapshot_task",
        "_hook_inbox_task",
        "_bin_freshness_task",
        "_zombie_messages_task",
        "_comms_messages_task",
        "_expired_isolation_task",
        "_vector_rebuild_task",
        "_memory_reconcile_task",
    ):
        await _cancel_runner_task(runner, attr)

    code_index_shutdown = getattr(runner, "_code_index_shutdown", None)
    if code_index_shutdown is not None:
        code_index_shutdown.set()
    await _cancel_runner_task(runner, "_code_index_task")

    sync_worker_shutdown = getattr(runner, "_sync_worker_shutdown", None)
    if sync_worker_shutdown is not None:
        sync_worker_shutdown.set()
    await _cancel_runner_task(runner, "_sync_worker_task", timeout=5.0)


async def _cleanup_pipeline_background_tasks() -> None:
    try:
        from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
            cleanup_background_tasks,
        )

        await asyncio.wait_for(cleanup_background_tasks(), timeout=5.0)
    except TimeoutError:
        logger.warning("Pipeline background tasks cleanup timed out")
    except Exception as e:
        logger.warning(f"Pipeline background tasks cleanup failed: {e}")


async def _stop_started_services(
    runner: GobbyRunner,
    cancel_active_agent_runs_for_shutdown: Callable[[GobbyRunner], Awaitable[int]],
    *,
    shutdown_intent: ShutdownIntent,
) -> None:
    if runner.agent_lifecycle_monitor:
        try:
            if shutdown_intent.cancel_agents:
                cancelled_runs = await cancel_active_agent_runs_for_shutdown(runner)
                if cancelled_runs > 0:
                    logger.info(
                        "Cancelled %d active agent run(s) during graceful shutdown",
                        cancelled_runs,
                    )
            else:
                logger.info("Preserving active agent runs during daemon restart")
            await asyncio.wait_for(runner.agent_lifecycle_monitor.stop(), timeout=2.0)
        except TimeoutError:
            logger.warning("Agent lifecycle monitor shutdown timed out")

    if runner.cron_scheduler:
        try:
            await asyncio.wait_for(runner.cron_scheduler.stop(), timeout=2.0)
        except TimeoutError:
            logger.warning("Cron scheduler shutdown timed out")

    if runner.message_processor:
        try:
            await asyncio.wait_for(runner.message_processor.stop(), timeout=2.0)
        except TimeoutError:
            logger.warning("Message processor shutdown timed out")

    if runner.communications_manager:
        try:
            await asyncio.wait_for(runner.communications_manager.stop(), timeout=5.0)
        except TimeoutError:
            logger.warning("CommunicationsManager shutdown timed out")


def _stop_ui_dev_server_if_needed(runner: GobbyRunner) -> None:
    if runner.config.ui.enabled and runner.config.ui.mode == "dev":
        from gobby.cli.utils import stop_ui_server

        stop_ui_server(quiet=True)


async def _close_managers_and_storage(runner: GobbyRunner) -> None:
    hook_manager = getattr(runner.http_server, "_hook_manager", None)
    if hook_manager:
        try:
            hook_manager.shutdown()
        except Exception as e:
            logger.warning(f"HookManager shutdown failed: {e}")

    if runner.memory_manager:
        try:
            await asyncio.wait_for(runner.memory_manager.close(), timeout=5.0)
        except TimeoutError:
            logger.warning("MemoryManager close timed out")
        except Exception as e:
            logger.warning(f"MemoryManager close failed: {e}")

    if runner.vector_store:
        try:
            await asyncio.wait_for(runner.vector_store.close(), timeout=5.0)
        except TimeoutError:
            logger.warning("VectorStore close timed out")
        except Exception as e:
            logger.warning(f"VectorStore close failed: {e}")


async def shutdown_daemon_services(
    runner: GobbyRunner,
    server: uvicorn.Server,
    server_task: asyncio.Task[Any],
    uvicorn_drain_timeout: int,
    *,
    await_critical_stop_hook_grace_window: Callable[[], Awaitable[None]],
    shutdown_websocket_server: Callable[[GobbyRunner], Awaitable[None]],
    cancel_active_agent_runs_for_shutdown: Callable[[GobbyRunner], Awaitable[int]],
    reap_remaining_child_processes: ReapChildProcesses,
    shutdown_telemetry: Callable[[], None],
    cleanup_pid_file: Callable[[], None],
) -> None:
    """Run the ordered graceful shutdown sequence."""
    shutdown_intent = coerce_shutdown_intent(getattr(runner, "_shutdown_intent", None))
    services = getattr(getattr(runner, "http_server", None), "services", None)
    if services is not None:
        services.startup_ready = False
        services.shutdown_in_progress = True
    await await_critical_stop_hook_grace_window()
    logger.debug("Shutdown requested; beginning graceful shutdown")
    server.should_exit = True
    try:
        await runner.http_server._terminate_streamable_http_sessions()
    except Exception as e:
        logger.warning(f"Failed to terminate Streamable HTTP sessions: {e}")

    await _cancel_runner_task(runner, "_subsystem_init_task")
    await _cancel_runner_task(runner, "_provider_model_refresh_task")

    await shutdown_websocket_server(runner)

    try:
        await asyncio.wait_for(runner.lifecycle_manager.stop(), timeout=2.0)
    except TimeoutError:
        logger.warning("Lifecycle manager shutdown timed out")

    try:
        logger.debug("Waiting for HTTP server lifespan shutdown")
        await asyncio.wait_for(server_task, timeout=uvicorn_drain_timeout + 5)
        logger.debug("HTTP server lifespan shutdown complete")
    except TimeoutError:
        logger.warning("HTTP server shutdown timed out")

    await _stop_started_services(
        runner,
        cancel_active_agent_runs_for_shutdown,
        shutdown_intent=shutdown_intent,
    )
    await _cleanup_pipeline_background_tasks()
    await _cancel_periodic_tasks(runner)
    _stop_ui_dev_server_if_needed(runner)
    await _close_managers_and_storage(runner)

    try:
        await asyncio.wait_for(runner.mcp_proxy.disconnect_all(), timeout=3.0)
    except TimeoutError:
        logger.warning("MCP disconnect timed out")

    preserved_agent_pids = (
        _preserved_agent_terminal_pids(runner) if shutdown_intent.preserve_agents else set()
    )
    await reap_remaining_child_processes(
        preserve_agents=shutdown_intent.preserve_agents,
        preserved_agent_pids=preserved_agent_pids,
    )

    try:
        shutdown_telemetry()
    except Exception as e:
        logger.warning(f"Telemetry shutdown failed: {e}")

    try:
        runner.database.close()
    except Exception as e:
        logger.warning(f"Database close failed: {e}")

    cleanup_pid_file()
    try:
        get_shutdown_marker_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug("Failed to remove shutdown marker during shutdown: %s", e)
    logger.info("Shutdown complete")
