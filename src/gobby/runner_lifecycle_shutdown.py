"""Graceful daemon shutdown helpers."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from gobby import runner_lifecycle_processes
from gobby.agents.terminal_delivery import (
    close_terminal_delivery_admission,
    detach_shielded_terminal_deliveries,
    drain_shielded_terminal_deliveries,
    reset_terminal_delivery_offload,
)
from gobby.mcp_proxy.tools.spawn_agent._health import cancel_and_await_health_checks
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
_HEALTH_CHECK_SETTLE_SECONDS = 2.0
_TERMINAL_DELIVERY_SETTLE_SECONDS = 6.0
_DATABASE_EXECUTOR_JOIN_SECONDS = 6.0
_FINALIZER_SETTLE_SECONDS = 10.0
_GOBBY_SHUTDOWN_DRAIN_MESSAGE = "Gobby shutdown drain"

# Set on the finalizer-deadline expiry branch: an abandoned settlement scope
# can leave a wedged non-daemon executor worker that blocks interpreter exit
# at the atexit thread join, so the standalone entry point must force process
# death after pid release. Embedded hosts never consult this.
_expiry_exit_backstop_required = False


def finalizer_expiry_backstop_required() -> bool:
    """Report whether shutdown abandoned unsettled terminal-delivery work."""
    return _expiry_exit_backstop_required


def _reset_finalizer_expiry_backstop() -> None:
    global _expiry_exit_backstop_required
    _expiry_exit_backstop_required = False


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
        "_test_schema_sweep_task",
        "_tool_results_cleanup_task",
        "_workflow_audit_cleanup_task",
        "_metrics_archive_task",
        "_model_metadata_refresh_task",
        "_provider_capability_refresh_task",
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

    external_issue_sync_shutdown = getattr(runner, "_external_issue_sync_shutdown", None)
    if external_issue_sync_shutdown is not None:
        external_issue_sync_shutdown.set()

    from gobby.providers.capabilities.refresh import CAPABILITY_REFRESH_DRAIN_TIMEOUT_SECONDS
    from gobby.runner_model_metadata_refresh import MODEL_METADATA_DRAIN_TIMEOUT_SECONDS

    cancellations = [
        (
            attr,
            _cancel_runner_task(
                runner,
                attr,
                timeout=(
                    CAPABILITY_REFRESH_DRAIN_TIMEOUT_SECONDS
                    if attr == "_provider_capability_refresh_task"
                    else (
                        MODEL_METADATA_DRAIN_TIMEOUT_SECONDS
                        if attr == "_model_metadata_refresh_task"
                        else 2.0
                    )
                ),
            ),
        )
        for attr in periodic_task_attrs
    ]
    cancellations.extend(
        (
            ("_code_index_task", _cancel_runner_task(runner, "_code_index_task")),
            (
                "_sync_worker_task",
                _cancel_runner_task(runner, "_sync_worker_task", timeout=5.0),
            ),
            (
                "_external_issue_sync_task",
                _cancel_runner_task(runner, "_external_issue_sync_task", timeout=10.0),
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
    *,
    shutdown_intent: ShutdownIntent,
) -> None:
    if runner.agent_lifecycle_monitor:
        logger.info("Preserving active agent runs during daemon shutdown")
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


def _log_shutdown_timeout(service_name: str, *, shutdown_intent: ShutdownIntent) -> None:
    if shutdown_intent is ShutdownIntent.RESTART:
        logger.info(
            "%s shutdown exceeded timeout during daemon restart; continuing with restart",
            service_name,
        )
    else:
        logger.warning("%s shutdown timed out", service_name)


def _stop_ui_dev_server_if_needed(runner: GobbyRunner) -> None:
    config = runner.startup_config
    if not config.ui.enabled:
        return

    from gobby.cli.ui_mode import resolve_ui_mode

    if resolve_ui_mode(config).effective == "dev":
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
            await asyncio.wait_for(memory_manager.close(), timeout=10.0)
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


async def _shutdown_database_executor(
    db_executor: Any,
    *,
    label: str = "Database executor",
    join_thread_name: str = "gobby-db-join",
    join_timeout_seconds: float | None = _DATABASE_EXECUTOR_JOIN_SECONDS,
) -> None:
    """Revoke queued thread work and join bounded operations off-loop."""
    if db_executor.is_joined():
        return
    try:
        db_executor.shutdown(cancel_futures=True)
    except Exception as e:
        logger.warning("%s shutdown failed: %s", label, e)
        return

    loop = asyncio.get_running_loop()
    joined: asyncio.Future[None] = loop.create_future()

    def finish_join(exc: Exception | None = None) -> None:
        if joined.done():
            return
        if exc is None:
            joined.set_result(None)
        else:
            joined.set_exception(exc)

    def join_executor() -> None:
        try:
            db_executor.join()
        except Exception as exc:
            loop.call_soon_threadsafe(finish_join, exc)
        else:
            loop.call_soon_threadsafe(finish_join)

    threading.Thread(target=join_executor, name=join_thread_name, daemon=True).start()
    try:
        if join_timeout_seconds is None:
            await joined
        else:
            await asyncio.wait_for(joined, timeout=join_timeout_seconds)
    except TimeoutError:
        logger.error("%s did not settle before the shutdown deadline", label)
    except Exception as e:
        logger.warning("%s join failed: %s", label, e)


async def _shutdown_database_concurrency(runner: GobbyRunner) -> None:
    watchdog = getattr(runner, "database_watchdog", None)
    if watchdog is not None:
        watchdog.stop()
    worktree_delete_executor = getattr(runner, "worktree_delete_executor", None)
    if worktree_delete_executor is not None:
        await _shutdown_database_executor(
            worktree_delete_executor,
            label="Worktree delete executor",
            join_thread_name="gobby-worktree-delete-join",
            join_timeout_seconds=None,
        )
    coverage_executor = getattr(runner, "coverage_executor", None)
    if coverage_executor is not None:
        await _shutdown_database_executor(
            coverage_executor,
            label="Coverage executor",
            join_thread_name="gobby-coverage-join",
        )
    db_executor = getattr(runner, "db_executor", None)
    if db_executor is not None:
        await _shutdown_database_executor(db_executor)


async def _settle_terminal_delivery_barrier() -> None:
    """Close delivery admission and settle all health and delivery producers."""
    close_terminal_delivery_admission()
    try:
        await asyncio.wait_for(
            cancel_and_await_health_checks(),
            timeout=_HEALTH_CHECK_SETTLE_SECONDS,
        )
    except TimeoutError:
        logger.error("Agent health checks did not settle before shutdown")

    try:
        await asyncio.wait_for(
            drain_shielded_terminal_deliveries(),
            timeout=_TERMINAL_DELIVERY_SETTLE_SECONDS,
        )
    except TimeoutError:
        detached = detach_shielded_terminal_deliveries()
        logger.error(
            "Detached overdue terminal deliveries for next-boot recovery: %s",
            detached,
        )


async def _run_terminal_delivery_finalizers(runner: GobbyRunner) -> None:
    """Settle delivery scopes and revoke/join their owned executor."""
    await _settle_terminal_delivery_barrier()
    await _shutdown_database_concurrency(runner)
    reset_terminal_delivery_offload()


async def _settle_finalizers_under_cancellation(
    runner: GobbyRunner,
) -> asyncio.CancelledError | None:
    """Defer caller cancellation until the bounded finalizer has settled."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _FINALIZER_SETTLE_SECONDS
    owned = asyncio.create_task(
        _run_terminal_delivery_finalizers(runner),
        name="terminal-delivery-finalizer",
    )
    cancellation: asyncio.CancelledError | None = None
    while not owned.done() and loop.time() < deadline:
        try:
            await asyncio.wait_for(
                asyncio.shield(owned),
                timeout=max(0.0, deadline - loop.time()),
            )
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except TimeoutError:
            break

    if owned.done():
        try:
            owned.result()
        except asyncio.CancelledError:
            logger.error("Terminal delivery finalizer was cancelled internally")
        except Exception:
            logger.warning("Terminal delivery finalizer failed", exc_info=True)
        return cancellation

    global _expiry_exit_backstop_required
    _expiry_exit_backstop_required = True
    detached = detach_shielded_terminal_deliveries()
    logger.error(
        "Terminal delivery finalizer exceeded %.1fs; detached work for next-boot recovery: %s",
        _FINALIZER_SETTLE_SECONDS,
        detached,
    )

    def consume_detached_result(task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    owned.add_done_callback(consume_detached_result)
    owned.cancel()
    watchdog = getattr(runner, "database_watchdog", None)
    if watchdog is not None:
        watchdog.stop()
    coverage_executor = getattr(runner, "coverage_executor", None)
    if coverage_executor is not None:
        try:
            coverage_executor.shutdown(cancel_futures=True)
        except Exception:
            logger.warning(
                "Coverage executor revocation failed after finalizer expiry", exc_info=True
            )
    db_executor = getattr(runner, "db_executor", None)
    if db_executor is not None:
        try:
            db_executor.shutdown(cancel_futures=True)
        except Exception:
            logger.warning(
                "Database executor revocation failed after finalizer expiry", exc_info=True
            )
    reset_terminal_delivery_offload()
    return cancellation


async def _drain_worktree_deletes_under_cancellation(
    runner: GobbyRunner,
    cancellation: asyncio.CancelledError | None,
) -> asyncio.CancelledError | None:
    """Finish destructive work before the database can close."""
    executor = getattr(runner, "worktree_delete_executor", None)
    if executor is None or executor.is_joined():
        return cancellation
    try:
        executor.shutdown(cancel_futures=True)
    except Exception:
        logger.warning("Worktree delete executor revocation failed", exc_info=True)

    drain = asyncio.create_task(
        asyncio.to_thread(executor.join),
        name="worktree-delete-final-drain",
    )
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    try:
        drain.result()
    except Exception:
        logger.warning("Worktree delete executor drain failed", exc_info=True)
    return cancellation


async def _run_graceful_shutdown_sequence(
    runner: GobbyRunner,
    server: uvicorn.Server,
    server_task: asyncio.Task[Any],
    uvicorn_drain_timeout: int,
    *,
    shutdown_intent: ShutdownIntent,
    await_critical_stop_hook_grace_window: Callable[[], Awaitable[None]],
    shutdown_websocket_server: Callable[[GobbyRunner], Awaitable[None]],
) -> None:
    from gobby.telemetry.rule_allow_audit import shutdown_rule_allow_audit

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

    if runner.communications_manager:
        await _best_effort(
            runner.communications_manager.stop,
            "CommunicationsManager shutdown",
            timeout=5.0,
        )

    await _best_effort(
        lambda: _drain_uvicorn_http_connections(server),
        "HTTP connection drain",
    )

    await _best_effort(
        lambda: shutdown_websocket_server(runner),
        "WebSocket server shutdown",
    )

    server.should_exit = True

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
    await _best_effort(shutdown_rule_allow_audit, "Rule allow audit drain")
    from gobby.runner_lifecycle_terminal_effects import (
        bridge_from_runner,
        drain_terminal_effects,
    )

    timeout = 5.0
    terminal_config = getattr(runner, "terminal_config", None)
    if terminal_config is not None:
        timeout = float(getattr(terminal_config, "hook_write_shutdown_timeout_seconds", timeout))
    await _best_effort(
        lambda: drain_terminal_effects(
            bridge_from_runner(runner),
            timeout_seconds=timeout,
        ),
        "Terminal effect drain",
        timeout=timeout + 1.0,
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
    from gobby.telemetry.rule_allow_audit import shutdown_rule_allow_audit

    await _settle_terminal_delivery_barrier()
    await _best_effort(shutdown_rule_allow_audit, "Rule allow audit drain")
    config_runtime = getattr(runner, "config_runtime", None)
    if config_runtime is not None:
        await _best_effort(config_runtime.close, "Config runtime shutdown")
    definition_revision_listener = getattr(runner, "definition_revision_listener", None)
    if definition_revision_listener is not None:
        await _best_effort(
            definition_revision_listener.close,
            "Definition revision listener shutdown",
        )
    preserved_agent_pids = await runner_lifecycle_processes._preserved_agent_terminal_pids(runner)
    if preserved_agent_pids is None:
        logger.warning(
            "Skipping child process reap: managed agent runs could not be "
            "enumerated, so reaping could kill live agents"
        )
    else:
        await _best_effort(
            lambda: reap_remaining_child_processes(
                preserve_agents=True,
                preserved_agent_pids=preserved_agent_pids,
            ),
            "Child process reap",
        )
    _best_effort_sync(shutdown_telemetry, "Telemetry shutdown")
    await _best_effort(
        lambda: _shutdown_database_concurrency(runner),
        "Database concurrency shutdown",
    )
    reset_terminal_delivery_offload()


async def shutdown_daemon_services(
    runner: GobbyRunner,
    server: uvicorn.Server,
    server_task: asyncio.Task[Any],
    uvicorn_drain_timeout: int,
    *,
    await_critical_stop_hook_grace_window: Callable[[], Awaitable[None]],
    shutdown_websocket_server: Callable[[GobbyRunner], Awaitable[None]],
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
        deferred_cancellation = await _settle_finalizers_under_cancellation(runner)
        deferred_cancellation = await _drain_worktree_deletes_under_cancellation(
            runner,
            deferred_cancellation,
        )

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
        if deferred_cancellation is not None:
            raise deferred_cancellation
    logger.info("Shutdown complete")
