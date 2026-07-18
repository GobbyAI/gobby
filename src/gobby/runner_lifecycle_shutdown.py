"""Graceful daemon shutdown helpers."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from gobby.runner_lifecycle_agents import _list_active_agent_runs_once
from gobby.shutdown_intent import ShutdownIntent, coerce_shutdown_intent, get_shutdown_marker_path

if TYPE_CHECKING:
    import uvicorn

    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")
WIKI_WATCHER_STOP_TIMEOUT_SECONDS: float = 2.0

_CRITICAL_STOP_HOOK_GRACE_SECONDS = 5.0
_HTTP_CONNECTION_DRAIN_SECONDS = 3.0
_HTTP_CONNECTION_GRACE_SECONDS = 0.25
_HTTP_REQUEST_TASK_CANCEL_TIMEOUT_SECONDS = 1.0
_GRACEFUL_SHUTDOWN_BUDGET_SECONDS = 14.0
_OVERALL_SHUTDOWN_DEADLINE_SECONDS = 17.0
_GOBBY_SHUTDOWN_DRAIN_MESSAGE = "Gobby shutdown drain"


class ReapChildProcesses(Protocol):
    async def __call__(
        self,
        *,
        preserve_agents: bool = False,
        preserved_agent_pids: set[int] | None = None,
    ) -> None: ...


async def _best_effort[T](
    operation: Callable[[], Awaitable[T]],
    name: str,
    *,
    timeout: float | None = None,
    on_timeout: Callable[[], None] | None = None,
) -> T | None:
    """Run one async shutdown step without letting ordinary failures stop the tail.

    ``CancelledError`` deliberately propagates so cancellation semantics remain
    visible to the caller and synchronous finalizers can run from ``finally``.
    """
    try:
        awaitable = operation()
        if timeout is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError:
        if on_timeout is not None:
            on_timeout()
        else:
            logger.warning("%s timed out", name)
    except Exception as e:
        logger.warning("%s failed: %s", name, e, exc_info=True)
    return None


def _best_effort_sync[T](operation: Callable[[], T], name: str) -> T | None:
    """Run one synchronous shutdown step and log ordinary failures."""
    try:
        return operation()
    except Exception as e:
        logger.warning("%s failed: %s", name, e, exc_info=True)
        return None


async def _preserved_agent_terminal_pids(runner: GobbyRunner) -> set[int]:
    """Return pane PIDs for active tmux-backed agents that survive restart."""
    agent_runner = getattr(runner, "agent_runner", None)
    run_storage = getattr(agent_runner, "run_storage", None)
    if run_storage is None:
        return set()
    try:
        db_executor = getattr(runner, "db_executor", None)
        if db_executor is not None:
            runs = await db_executor.run(_list_active_agent_runs_once, runner)
        else:
            runs = await asyncio.to_thread(_list_active_agent_runs_once, runner)
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
            logger.warning("WebSocket startup task failed during shutdown: %s", e)

    if websocket_server and getattr(websocket_server, "_server", None) is not None:
        logger.debug("Stopping WebSocket server before HTTP shutdown")
        try:
            await asyncio.wait_for(websocket_server.stop(), timeout=timeout)
            logger.debug("WebSocket server stopped before HTTP shutdown")
        except TimeoutError:
            logger.warning("WebSocket server shutdown timed out")
        except Exception as e:
            logger.warning("WebSocket server shutdown failed: %s", e)

    if websocket_task is not None:
        runner._websocket_task = None


async def _drain_uvicorn_http_connections(server: uvicorn.Server) -> None:
    """Ask uvicorn HTTP connections to close and cancel remaining request tasks."""
    state = getattr(server, "server_state", None)
    connections = getattr(state, "connections", None)
    tasks = getattr(state, "tasks", None)
    if connections is None and tasks is None:
        return

    if connections:
        for connection in list(connections):
            shutdown = getattr(connection, "shutdown", None)
            if callable(shutdown):
                shutdown()

    if await _wait_for_uvicorn_http_drain(
        connections,
        tasks,
        timeout=_HTTP_CONNECTION_GRACE_SECONDS,
    ):
        return

    if connections:
        for connection in list(connections):
            transport = getattr(connection, "transport", None)
            close = getattr(transport, "close", None)
            is_closing = getattr(transport, "is_closing", None)
            if callable(close) and not (callable(is_closing) and is_closing()):
                close()

    drained = await _wait_for_uvicorn_http_drain(
        connections,
        tasks,
        timeout=_HTTP_CONNECTION_DRAIN_SECONDS,
    )

    if drained:
        return

    remaining_connections = len(connections or ())
    remaining_tasks = len(_live_uvicorn_http_tasks(tasks))

    if remaining_tasks:
        await _cancel_remaining_request_tasks(tasks)
        remaining_connections = len(connections or ())
        remaining_tasks = len(_live_uvicorn_http_tasks(tasks))

    logger.debug(
        "HTTP request drain left %d connection(s) and %d live task(s) after cancellation",
        remaining_connections,
        remaining_tasks,
    )


async def _wait_for_uvicorn_http_drain(
    connections: Any,
    tasks: Any,
    *,
    timeout: float,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if not connections and not tasks:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)


async def _cancel_remaining_request_tasks(tasks: Any) -> None:
    """Cancel any remaining HTTP/MCP request tasks during shutdown."""
    task_list = _live_uvicorn_http_tasks(tasks)
    if not task_list:
        return

    for task in task_list:
        task.cancel(_GOBBY_SHUTDOWN_DRAIN_MESSAGE)

    deadline = asyncio.get_running_loop().time() + _HTTP_REQUEST_TASK_CANCEL_TIMEOUT_SECONDS
    while True:
        if not _live_uvicorn_http_tasks(tasks):
            return
        if asyncio.get_running_loop().time() >= deadline:
            return
        await asyncio.sleep(0.05)


def _live_uvicorn_http_tasks(tasks: Any) -> list[asyncio.Task[Any]]:
    if not tasks:
        return []
    return [task for task in list(tasks) if isinstance(task, asyncio.Task) and not task.done()]


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
        logger.warning("Child process reap failed: %s", e)


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
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
        except TimeoutError:
            pass


async def _cancel_periodic_tasks(runner: GobbyRunner) -> None:
    wiki_watcher = getattr(runner, "_wiki_watcher", None)
    if wiki_watcher is not None:
        try:
            await asyncio.wait_for(
                wiki_watcher.stop(),
                timeout=WIKI_WATCHER_STOP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("Wiki watcher shutdown timed out")
        except Exception as e:
            logger.warning("Wiki watcher shutdown failed: %s", e)

    periodic_task_attrs = (
        "_metrics_cleanup_task",
        "_workflow_audit_cleanup_task",
        "_metrics_archive_task",
        "_span_cleanup_task",
        "_unmodeled_observations_cleanup_task",
        "_metric_snapshot_task",
        "_resource_monitor_task",
        "_hook_inbox_task",
        "_bin_freshness_task",
        "_approval_timeout_task",
        "_zombie_messages_task",
        "_comms_messages_task",
        "_skill_purge_task",
        "_chat_attachments_cleanup_task",
        "_expired_isolation_task",
        "_vector_rebuild_task",
        "_memory_reconcile_task",
        "_recall_drift_task",
        "_tmux_window_repair_task",
        "_wiki_watcher_task",
    )

    code_index_shutdown = getattr(runner, "_code_index_shutdown", None)
    if code_index_shutdown is not None:
        code_index_shutdown.set()

    sync_worker_shutdown = getattr(runner, "_sync_worker_shutdown", None)
    if sync_worker_shutdown is not None:
        sync_worker_shutdown.set()

    cancellations = [(attr, _cancel_runner_task(runner, attr)) for attr in periodic_task_attrs]
    cancellations.extend(
        (
            ("_code_index_task", _cancel_runner_task(runner, "_code_index_task")),
            (
                "_sync_worker_task",
                _cancel_runner_task(runner, "_sync_worker_task", timeout=5.0),
            ),
        )
    )
    results = await asyncio.gather(
        *(cancellation for _, cancellation in cancellations),
        return_exceptions=True,
    )
    for (attr, _), result in zip(cancellations, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("Failed to cancel periodic task %s: %r", attr, result)

    if hasattr(runner, "_wiki_watcher_task"):
        runner._wiki_watcher_task = None
    if hasattr(runner, "_wiki_watcher"):
        runner._wiki_watcher = None


async def _cleanup_pipeline_background_tasks() -> None:
    try:
        from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
            cleanup_background_tasks,
        )

        await asyncio.wait_for(cleanup_background_tasks(), timeout=5.0)
    except TimeoutError:
        logger.warning("Pipeline background tasks cleanup timed out")
    except Exception as e:
        logger.warning("Pipeline background tasks cleanup failed: %s", e)


async def _stop_started_services(
    runner: GobbyRunner,
    cancel_active_agent_runs_for_shutdown: Callable[[GobbyRunner], Awaitable[int]],
    *,
    shutdown_intent: ShutdownIntent,
) -> None:
    if runner.agent_lifecycle_monitor:
        if shutdown_intent.cancel_agents:
            cancelled_runs = await _best_effort(
                lambda: cancel_active_agent_runs_for_shutdown(runner),
                "Active agent run cancellation",
            )
            if cancelled_runs is not None and cancelled_runs > 0:
                logger.info(
                    "Cancelled %d active agent run(s) during graceful shutdown",
                    cancelled_runs,
                )
        else:
            logger.info("Preserving active agent runs during daemon restart")
        await _best_effort(
            runner.agent_lifecycle_monitor.stop,
            "Agent lifecycle monitor shutdown",
            timeout=2.0,
        )

    if runner.cron_scheduler:
        await _best_effort(
            runner.cron_scheduler.stop,
            "Cron scheduler shutdown",
            timeout=2.0,
            on_timeout=lambda: _log_shutdown_timeout(
                "Cron scheduler",
                shutdown_intent=shutdown_intent,
            ),
        )

    system_automation_loop = getattr(runner, "system_automation_loop", None)
    if system_automation_loop is not None:
        await _best_effort(
            system_automation_loop.stop,
            "System automation loop shutdown",
            timeout=2.0,
            on_timeout=lambda: _log_shutdown_timeout(
                "System automation loop",
                shutdown_intent=shutdown_intent,
            ),
        )

    if runner.message_processor:
        await _best_effort(
            runner.message_processor.stop,
            "Message processor shutdown",
            timeout=2.0,
        )

    if runner.communications_manager:
        await _best_effort(
            runner.communications_manager.stop,
            "CommunicationsManager shutdown",
            timeout=5.0,
        )


def _log_shutdown_timeout(service_name: str, *, shutdown_intent: ShutdownIntent) -> None:
    if shutdown_intent is ShutdownIntent.RESTART:
        logger.info(
            "%s shutdown exceeded timeout during daemon restart; continuing with restart",
            service_name,
        )
    else:
        logger.warning("%s shutdown timed out", service_name)


def _stop_ui_dev_server_if_needed(runner: GobbyRunner) -> None:
    if not runner.config.ui.enabled:
        return

    from gobby.cli.ui_mode import resolve_ui_mode

    if resolve_ui_mode(runner.config).effective == "dev":
        from gobby.cli.utils import stop_ui_server

        stop_ui_server(quiet=True)


async def _close_managers_and_storage(runner: GobbyRunner) -> None:
    hook_manager = getattr(runner.http_server, "_hook_manager", None)
    if hook_manager:
        if getattr(hook_manager, "_shutdown_complete", False):
            logger.debug("HookManager shutdown already handled by HTTP lifespan")
        else:
            try:
                await hook_manager.shutdown_async()
            except Exception as e:
                logger.warning("HookManager shutdown failed: %s", e)
            else:
                if getattr(runner.http_server, "_hook_manager", None) is hook_manager:
                    runner.http_server._hook_manager = None

    memory_manager = getattr(runner, "memory_manager", None)
    if memory_manager:
        try:
            await asyncio.wait_for(memory_manager.close(), timeout=5.0)
        except TimeoutError:
            logger.warning("MemoryManager close timed out")
        except Exception as e:
            logger.warning("MemoryManager close failed: %s", e)

    vector_store = getattr(runner, "vector_store", None)
    if vector_store:
        try:
            await asyncio.wait_for(vector_store.close(), timeout=5.0)
        except TimeoutError:
            logger.warning("VectorStore close timed out")
        except Exception as e:
            logger.warning("VectorStore close failed: %s", e)


async def _shutdown_database_executor(db_executor: Any) -> None:
    """Stop queued database work without stranding the event-loop executor."""
    try:
        # ThreadPoolExecutor.shutdown(wait=False) is non-blocking. Running
        # operations are allowed to finish while queued operations are
        # cancelled. Do not put wait=True in asyncio's default executor: a
        # timed-out to_thread call keeps running, and asyncio.run() waits for
        # that worker again while closing the event loop.
        db_executor.shutdown(wait=False, cancel_futures=True)
    except Exception as e:
        logger.warning("Database executor shutdown failed: %s", e)


async def _run_graceful_shutdown_sequence(
    runner: GobbyRunner,
    server: uvicorn.Server,
    server_task: asyncio.Task[Any],
    uvicorn_drain_timeout: int,
    *,
    shutdown_intent: ShutdownIntent,
    await_critical_stop_hook_grace_window: Callable[[], Awaitable[None]],
    shutdown_websocket_server: Callable[[GobbyRunner], Awaitable[None]],
    cancel_active_agent_runs_for_shutdown: Callable[[GobbyRunner], Awaitable[int]],
) -> None:
    if shutdown_intent is ShutdownIntent.STOP:
        await _best_effort(
            await_critical_stop_hook_grace_window,
            "Critical Stop-hook grace window",
        )
    else:
        logger.debug("Skipping critical Stop-hook grace during daemon restart")
    logger.debug("Shutdown requested; beginning graceful shutdown")

    cleanup_pending_interactions = getattr(
        getattr(runner, "http_server", None),
        "_cleanup_pending_interactions",
        None,
    )
    if cleanup_pending_interactions is not None:
        await _best_effort(
            cleanup_pending_interactions,
            "Pending interaction cleanup",
        )

    await _best_effort(
        runner.http_server._terminate_streamable_http_sessions,
        "Streamable HTTP session termination",
    )

    await _best_effort(
        lambda: _drain_uvicorn_http_connections(server),
        "HTTP connection drain",
    )

    server.should_exit = True

    await _best_effort(
        lambda: _cancel_runner_task(runner, "_provider_model_refresh_task"),
        "Provider model refresh task cancellation",
    )

    await _best_effort(
        lambda: shutdown_websocket_server(runner),
        "WebSocket server shutdown",
    )

    await _best_effort(
        runner.lifecycle_manager.stop,
        "Lifecycle manager shutdown",
        timeout=2.0,
        on_timeout=lambda: _log_shutdown_timeout(
            "Lifecycle manager",
            shutdown_intent=shutdown_intent,
        ),
    )

    async def wait_for_http_server_shutdown() -> None:
        logger.debug("Waiting for HTTP server lifespan shutdown")
        await server_task
        logger.debug("HTTP server lifespan shutdown complete")

    await _best_effort(
        wait_for_http_server_shutdown,
        "HTTP server shutdown",
        timeout=uvicorn_drain_timeout + 5,
    )
    await _best_effort(
        lambda: _stop_started_services(
            runner,
            cancel_active_agent_runs_for_shutdown,
            shutdown_intent=shutdown_intent,
        ),
        "Started service shutdown",
    )
    await _best_effort(
        _cleanup_pipeline_background_tasks,
        "Pipeline background task cleanup",
    )
    await _best_effort(
        lambda: _cancel_periodic_tasks(runner),
        "Periodic task cancellation",
    )
    _best_effort_sync(
        lambda: _stop_ui_dev_server_if_needed(runner),
        "UI development server shutdown",
    )
    await _best_effort(
        lambda: _close_managers_and_storage(runner),
        "Manager and storage shutdown",
    )
    await _best_effort(
        runner.mcp_proxy.disconnect_all,
        "MCP disconnect",
        timeout=3.0,
    )


async def _run_async_shutdown_cleanup(
    runner: GobbyRunner,
    *,
    shutdown_intent: ShutdownIntent,
    reap_remaining_child_processes: ReapChildProcesses,
    shutdown_telemetry: Callable[[], None],
) -> None:
    """Run bounded asynchronous cleanup before the synchronous finalizers."""
    preserved_agent_pids = (
        await _preserved_agent_terminal_pids(runner) if shutdown_intent.preserve_agents else set()
    )
    await _best_effort(
        lambda: reap_remaining_child_processes(
            preserve_agents=shutdown_intent.preserve_agents,
            preserved_agent_pids=preserved_agent_pids,
        ),
        "Child process reap",
    )
    _best_effort_sync(shutdown_telemetry, "Telemetry shutdown")

    db_executor = getattr(runner, "db_executor", None)
    if db_executor is not None:
        await _best_effort(
            lambda: _shutdown_database_executor(db_executor),
            "Database executor shutdown",
        )


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
    """Run graceful shutdown within the CLI's process-termination deadline."""
    shutdown_intent = coerce_shutdown_intent(getattr(runner, "_shutdown_intent", None))
    services = getattr(getattr(runner, "http_server", None), "services", None)
    if services is not None:
        services.startup_ready = False
        services.shutdown_in_progress = True

    loop = asyncio.get_running_loop()
    overall_deadline = loop.time() + _OVERALL_SHUTDOWN_DEADLINE_SECONDS
    graceful_deadline = min(
        overall_deadline,
        loop.time() + _GRACEFUL_SHUTDOWN_BUDGET_SECONDS,
    )
    overall_timeout = asyncio.timeout_at(overall_deadline)
    try:
        try:
            async with overall_timeout:
                await _best_effort(
                    lambda: _cancel_runner_task(runner, "_subsystem_init_task"),
                    "Subsystem initialization task cancellation",
                )
                graceful_timeout = asyncio.timeout_at(graceful_deadline)
                try:
                    async with graceful_timeout:
                        await _best_effort(
                            lambda: _run_graceful_shutdown_sequence(
                                runner,
                                server,
                                server_task,
                                uvicorn_drain_timeout,
                                shutdown_intent=shutdown_intent,
                                await_critical_stop_hook_grace_window=(
                                    await_critical_stop_hook_grace_window
                                ),
                                shutdown_websocket_server=shutdown_websocket_server,
                                cancel_active_agent_runs_for_shutdown=(
                                    cancel_active_agent_runs_for_shutdown
                                ),
                            ),
                            "Graceful shutdown sequence",
                        )
                except TimeoutError:
                    if not graceful_timeout.expired():
                        raise
                    logger.warning(
                        "Graceful shutdown exceeded %.1fs budget; entering cleanup tail",
                        _GRACEFUL_SHUTDOWN_BUDGET_SECONDS,
                    )

                await _run_async_shutdown_cleanup(
                    runner,
                    shutdown_intent=shutdown_intent,
                    reap_remaining_child_processes=reap_remaining_child_processes,
                    shutdown_telemetry=shutdown_telemetry,
                )
        except TimeoutError:
            if not overall_timeout.expired():
                raise
            logger.warning(
                "Async shutdown cleanup exceeded %.1fs overall deadline",
                _OVERALL_SHUTDOWN_DEADLINE_SECONDS,
            )
    finally:
        try:
            runner.database.close()
        except Exception as e:
            logger.warning("Database close failed: %s", e)

        try:
            cleanup_pid_file()
        except Exception as e:
            logger.warning("PID file cleanup failed: %s", e)
        finally:
            if shutdown_intent is not ShutdownIntent.RESTART:
                try:
                    get_shutdown_marker_path().unlink()
                except FileNotFoundError:
                    pass
                except OSError as e:
                    logger.debug("Failed to remove shutdown marker during shutdown: %s", e)
    logger.info("Shutdown complete")
