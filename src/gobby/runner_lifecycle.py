"""GobbyRunner daemon lifecycle — startup, event loop, and shutdown.

Extracted from runner.py to keep the main module under the monolith limit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn

from gobby.telemetry import shutdown_telemetry

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)

_CRITICAL_STOP_HOOK_GRACE_SECONDS = 5.0

# ---------------------------------------------------------------------------
# Startup progress tracking (module-level so the admin API can read it)
# ---------------------------------------------------------------------------

_startup_tracker: StartupTracker | None = None


class StartupTracker:
    """Tracks subsystem initialization progress for CLI polling."""

    __slots__ = ("steps_completed", "steps_scheduled", "errors", "done", "started_at")

    def __init__(self) -> None:
        self.steps_completed: list[str] = []
        self.steps_scheduled: list[str] = []
        self.errors: list[dict[str, str]] = []
        self.done: bool = False
        self.started_at: float = time.monotonic()

    def complete(self, step: str) -> None:
        self.steps_completed.append(step)

    def schedule(self, step: str) -> None:
        self.steps_scheduled.append(step)

    def error(self, subsystem: str, error: str) -> None:
        self.errors.append({"subsystem": subsystem, "error": error})

    def finish(self) -> None:
        self.done = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps_completed": list(self.steps_completed),
            "steps_scheduled": list(self.steps_scheduled),
            "errors": list(self.errors),
            "done": self.done,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 1),
        }


def get_startup_tracker() -> StartupTracker | None:
    """Return the current startup tracker (used by admin API)."""
    return _startup_tracker


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


def _register_persisted_completion_subscribers(
    runner: GobbyRunner,
    completion_id: str,
    *,
    continuation_prompt: str | None = None,
) -> list[str]:
    """Load persisted waiters for a completion ID into the in-memory registry."""
    if not runner.pipeline_execution_manager or not runner.completion_registry:
        return []

    subscribers = runner.pipeline_execution_manager.get_completion_subscribers(completion_id)
    if subscribers:
        runner.completion_registry.register(
            completion_id,
            subscribers=subscribers,
            continuation_prompt=continuation_prompt,
        )
    return subscribers


def _cleanup_persisted_completion_subscribers(
    runner: GobbyRunner,
    completion_id: str,
    subscribers: list[str],
) -> None:
    """Drop persisted/in-memory subscriber state after a restart notification."""
    if not subscribers:
        return
    if runner.pipeline_execution_manager:
        runner.pipeline_execution_manager.remove_completion_subscribers(completion_id)
    if runner.completion_registry:
        runner.completion_registry.cleanup(completion_id)


async def _recover_agent_runs_after_restart(runner: GobbyRunner) -> int:
    """Rehydrate completion events for active agent rows after daemon restart."""
    if runner.agent_runner is None or runner.completion_registry is None:
        return 0

    rehydrated = 0
    for run in runner.agent_runner.run_storage.list_active(limit=1000):
        if runner.completion_registry.is_registered(run.id):
            continue
        subscribers: list[str] = []
        if runner.pipeline_execution_manager:
            subscribers = runner.pipeline_execution_manager.get_completion_subscribers(run.id)
        runner.completion_registry.register(
            run.id,
            subscribers=subscribers,
            continuation_prompt=getattr(run, "continuation_prompt", None),
        )
        rehydrated += 1

    return rehydrated


async def _replay_daemon_restart_agent_cancellations(runner: GobbyRunner) -> int:
    """Replay durable wake notifications for daemon-restart agent cancellations."""
    if (
        runner.agent_runner is None
        or runner.pipeline_execution_manager is None
        or runner.completion_registry is None
    ):
        return 0

    replayed = 0
    cancelled_runs = runner.agent_runner.run_storage.list_by_status("cancelled", limit=1000)
    for run in cancelled_runs:
        if getattr(run, "terminal_reason", None) != "daemon_restart":
            continue

        subscribers = runner.pipeline_execution_manager.get_completion_subscribers(run.id)
        if not subscribers:
            continue

        if not runner.completion_registry.is_registered(run.id):
            runner.completion_registry.register(
                run.id,
                subscribers=subscribers,
                continuation_prompt=getattr(run, "continuation_prompt", None),
            )

        try:
            await runner.completion_registry.notify(
                run.id,
                result={
                    "status": "cancelled",
                    "terminal_reason": "daemon_restart",
                    "run_id": run.id,
                    "completion_id": run.id,
                },
                message=(
                    f"Agent {run.id} was interrupted by a daemon restart.\n"
                    "Status: cancelled (daemon restarted)"
                ),
            )
        except Exception as e:
            logger.warning(
                "Failed to replay daemon-restart cancellation for agent %s: %s",
                run.id,
                e,
            )
            continue

        _cleanup_persisted_completion_subscribers(runner, run.id, subscribers)
        replayed += 1

    return replayed


async def _cancel_active_agent_runs_for_shutdown(runner: GobbyRunner) -> int:
    """Cancel live agent runs before subsystem teardown on daemon shutdown."""
    if runner.agent_lifecycle_monitor is None or runner.agent_runner is None:
        return 0

    from gobby.agents.kill import kill_agent as _kill_agent_process

    cancelled = 0
    for run in runner.agent_runner.run_storage.list_active(limit=1000):
        _register_persisted_completion_subscribers(
            runner,
            run.id,
            continuation_prompt=getattr(run, "continuation_prompt", None),
        )
        result = await _kill_agent_process(
            run,
            runner.database,
            signal_name="TERM",
            close_terminal=True,
        )
        if not result.get("success") and result.get("error") != "No target PID found":
            logger.warning(
                "Failed to stop active agent %s during shutdown: %s",
                run.id,
                result.get("error"),
            )
            continue

        transitioned = await runner.agent_lifecycle_monitor.terminalize_cancelled_run(
            run.id,
            terminal_reason="daemon_restart",
        )
        if transitioned:
            cancelled += 1

    return cancelled


async def _reap_remaining_child_processes(timeout: float = 1.0) -> None:
    """Terminate then force-kill child processes that survived graceful shutdown."""
    try:
        import psutil

        current_process = psutil.Process(os.getpid())
        children = current_process.children(recursive=True)
        if not children:
            logger.debug("No child processes remaining after graceful shutdown")
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


async def _init_subsystems(runner: GobbyRunner, rebuild_vector_store: Any) -> None:
    """Heavy initialization that runs after HTTP is already serving.

    All work here is non-critical for the health endpoint — subsystems
    come online progressively while the daemon is already reachable.
    """
    global _startup_tracker
    tracker = _startup_tracker

    provider_catalog = getattr(runner.http_server.services, "provider_model_catalog", None)
    if provider_catalog is not None:
        try:
            status = await provider_catalog.refresh(codex_client=runner.http_server.codex_client)
            if tracker:
                tracker.complete("Provider model catalogs updated")
                for provider, info in status.items():
                    source = info.get("source", "failed")
                    error = info.get("error")
                    if source == "live":
                        continue
                    if source == "cache":
                        tracker.error(
                            f"Provider models ({provider})",
                            f"using cache: {error or 'live probe failed'}",
                        )
                    else:
                        tracker.error(
                            f"Provider models ({provider})",
                            error or "model discovery failed",
                        )
        except Exception as e:
            logger.warning(f"Provider model discovery failed: {e}")
            if tracker:
                tracker.error("Provider models", str(e))

    # Connect MCP servers
    try:
        await asyncio.wait_for(runner.mcp_proxy.connect_all(), timeout=10.0)
        if tracker:
            tracker.complete("MCP servers connected")
    except TimeoutError:
        logger.warning("MCP connection timed out")
        if tracker:
            tracker.error("MCP servers", "connection timed out")
    except Exception as e:
        logger.error(f"MCP connection failed: {e}")
        if tracker:
            tracker.error("MCP servers", str(e))

    # Qdrant health check: disable vector features if unreachable
    db_cfg = runner.config.databases
    if db_cfg.qdrant.url:
        from gobby.cli.services import is_qdrant_healthy

        if not await is_qdrant_healthy(db_cfg.qdrant.url):
            logger.warning(
                f"Qdrant configured but unreachable at {db_cfg.qdrant.url} — vector features disabled"
            )
            runner.vector_store = None
            if tracker:
                tracker.error("Qdrant", f"unreachable at {db_cfg.qdrant.url}")
        elif tracker:
            tracker.complete("Qdrant healthy")

    # Neo4j health check: disable KG features if unreachable
    if runner.memory_manager and db_cfg.neo4j.url:
        from gobby.cli.services import is_neo4j_healthy

        if not await is_neo4j_healthy(db_cfg.neo4j.url):
            logger.warning(
                f"Neo4j configured but unreachable at {db_cfg.neo4j.url} — graph features disabled"
            )
            runner.memory_manager.clear_graph_clients()
            if tracker:
                tracker.error("Neo4j", f"unreachable at {db_cfg.neo4j.url}")
        elif tracker:
            tracker.complete("Neo4j healthy")

    # Embedding health check: probe endpoint, attempt auto-load, warn if down
    emb_cfg = runner.config.embeddings
    if emb_cfg.api_base:
        from gobby.cli.services import (
            ensure_local_embedding_service_ready,
            get_local_embedding_service_failure_reason,
        )

        healthy = await ensure_local_embedding_service_ready(
            model=emb_cfg.model,
            api_base=emb_cfg.api_base,
            api_key=emb_cfg.api_key,
            expected_dim=emb_cfg.dim,
        )
        if healthy:
            if tracker:
                tracker.complete("Embeddings healthy")
        else:
            failure_reason = get_local_embedding_service_failure_reason()
            if failure_reason:
                logger.warning(
                    "Embedding readiness failed at %s (model: %s): %s",
                    emb_cfg.api_base,
                    emb_cfg.model,
                    failure_reason,
                )
            else:
                failure_reason = f"unreachable at {emb_cfg.api_base}"
                logger.warning(
                    f"Embedding endpoint unreachable at {emb_cfg.api_base} "
                    f"(model: {emb_cfg.model}) — semantic search will fall back to FTS5"
                )
            if tracker:
                tracker.error("Embeddings", failure_reason)

    # Run metrics cleanup on startup
    try:
        deleted = runner.metrics_manager.cleanup_old_metrics()
        if deleted > 0:
            logger.info(f"Startup metrics cleanup: removed {deleted} old entries")
    except Exception as e:
        logger.warning(f"Metrics cleanup failed: {e}")

    # Initialize VectorStore and schedule rebuild in background if needed
    if runner.vector_store:
        try:
            await runner.vector_store.initialize()
            from gobby.mcp_proxy.semantic_search import SemanticToolSearch

            await runner.vector_store.ensure_collection(
                SemanticToolSearch.TOOL_COLLECTION,
                runner.config.embeddings.dim,
            )
            qdrant_count = await runner.vector_store.count()
            if qdrant_count == 0 and runner.memory_manager:
                sqlite_memories = runner.memory_manager.storage.list_memories(limit=10000)
                if sqlite_memories:
                    embed_fn = runner.memory_manager.embed_fn
                    if embed_fn:
                        logger.info(
                            f"Qdrant empty, scheduling background rebuild from "
                            f"{len(sqlite_memories)} SQLite memories..."
                        )
                        memory_dicts = [{"id": m.id, "content": m.content} for m in sqlite_memories]
                        runner._vector_rebuild_task = asyncio.create_task(
                            rebuild_vector_store(runner.vector_store, memory_dicts, embed_fn),
                            name="vector-store-rebuild",
                        )
                        if tracker:
                            tracker.schedule(
                                f"Vector store rebuild ({len(sqlite_memories)} memories)"
                            )
                    else:
                        logger.warning("No embed_fn configured, skipping VectorStore rebuild")
            if tracker:
                tracker.complete("Vector store initialized")
        except Exception as e:
            logger.error(f"VectorStore initialization failed: {e}")
            if tracker:
                tracker.error("Vector store", str(e))

    # Start Message Processor
    if runner.message_processor:
        await runner.message_processor.start()
        if tracker:
            tracker.complete("Message processor")

    # Start Communications Manager
    if runner.communications_manager:
        try:
            await runner.communications_manager.start()
            if tracker:
                tracker.complete("Communications manager")
        except Exception as e:
            logger.error(f"CommunicationsManager start failed: {e}")
            if tracker:
                tracker.error("Communications manager", str(e))

    # Start Session Lifecycle Manager
    await runner.lifecycle_manager.start()
    if tracker:
        tracker.complete("Session lifecycle manager")

    # tmux socket health check before any agent operations
    try:
        from gobby.agents.tmux.session_manager import TmuxSessionManager

        tmux_mgr = TmuxSessionManager()
        await tmux_mgr.health_check()
        if tracker:
            tracker.complete("tmux healthy")
    except Exception as e:
        logger.warning(f"tmux health check failed on startup: {e}")
        if tracker:
            tracker.error("tmux", str(e))

    # Start agent lifecycle monitor
    if runner.agent_lifecycle_monitor:
        await runner.agent_lifecycle_monitor.cleanup_stale_pending_runs()
        rehydrated_runs = await _recover_agent_runs_after_restart(runner)
        if rehydrated_runs > 0:
            logger.info(
                "Rehydrated completion events for %d active agent run(s)",
                rehydrated_runs,
            )
        await runner.agent_lifecycle_monitor.start()
        replayed_cancellations = await _replay_daemon_restart_agent_cancellations(runner)
        if replayed_cancellations > 0:
            logger.info(
                "Replayed daemon-restart cancellation wakes for %d agent run(s)",
                replayed_cancellations,
            )
        if tracker:
            tracker.complete("Agent lifecycle monitor")

    # Start Cron Scheduler
    if runner.cron_scheduler:
        await runner.cron_scheduler.start()
        if tracker:
            tracker.complete("Cron scheduler")

    # Code index maintenance loop
    runner._code_index_task = None
    if runner.code_indexer:
        from gobby.code_index.maintenance import code_index_maintenance_loop

        # Build summarizer if LLM service is available and summaries are enabled
        summarizer = None
        if runner.config.code_index.summary_enabled and runner.llm_service is not None:
            from gobby.code_index.summarizer import SymbolSummarizer

            try:
                summarizer = SymbolSummarizer(runner.llm_service, runner.config.code_index)
            except Exception as e:
                logger.warning(f"Failed to create SymbolSummarizer: {e}")

        shutdown_event = asyncio.Event()
        runner._code_index_shutdown = shutdown_event
        runner._code_index_task = asyncio.create_task(
            code_index_maintenance_loop(
                context=runner.code_indexer,
                shutdown_flag=shutdown_event,
                interval=runner.config.code_index.maintenance_interval_seconds,
                summarizer=summarizer,
                summary_batch_size=runner.config.code_index.summary_batch_size,
            ),
            name="code-index-maintenance",
        )
        if tracker:
            tracker.schedule("Code index maintenance")

    # Code index sync worker (external store sync: Qdrant embeddings, Neo4j edges)
    runner._sync_worker_task = None
    if runner.code_indexer:
        from gobby.code_index.sync_worker import sync_worker_loop

        sync_shutdown = asyncio.Event()
        runner._sync_worker_shutdown = sync_shutdown
        runner._sync_worker_task = asyncio.create_task(
            sync_worker_loop(
                storage=runner.code_indexer.storage,
                vector_store=runner.vector_store,
                graph=runner.code_indexer.graph,
                config=runner.config.code_index,
                embeddings_config=runner.config.embeddings,
                shutdown_flag=sync_shutdown,
            ),
            name="code-index-sync-worker",
        )
        if tracker:
            tracker.schedule("Code index sync")

    # Resume interrupted pipelines and fail non-resumable stale executions
    if runner.pipeline_executor and runner.pipeline_execution_manager and runner.workflow_loader:
        try:
            from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
                resume_interrupted_pipelines,
            )

            resumed_ids = await resume_interrupted_pipelines(
                loader=runner.workflow_loader,
                executor=runner.pipeline_executor,
                execution_manager=runner.pipeline_execution_manager,
                project_id=runner.project_id,
            )
            if resumed_ids:
                logger.info(f"Resumed {len(resumed_ids)} pipeline(s) after restart: {resumed_ids}")

            stale_count = runner.pipeline_execution_manager.fail_stale_running_executions(
                exclude_ids=set(resumed_ids),
            )
            if stale_count > 0:
                logger.info(f"Failed {stale_count} non-resumable stale pipeline executions")

            # Wake subscribers of interrupted (non-resumed) pipelines
            if stale_count > 0 and runner.completion_registry:
                try:
                    from gobby.workflows.pipeline_state import ExecutionStatus as _ES

                    interrupted = runner.pipeline_execution_manager.list_executions(
                        status=_ES.INTERRUPTED,
                    )
                    for exe in interrupted:
                        subs = runner.pipeline_execution_manager.get_completion_subscribers(exe.id)
                        if subs:
                            runner.completion_registry.register(exe.id, subscribers=subs)
                            await runner.completion_registry.notify(
                                exe.id,
                                result={
                                    "status": "interrupted",
                                    "pipeline_name": exe.pipeline_name,
                                    "error": "Daemon restarted while execution was in progress",
                                },
                                message=(
                                    f'[Completion Notification] Pipeline "{exe.pipeline_name}" '
                                    f"({exe.id}) was interrupted.\n"
                                    f"Status: interrupted (daemon restarted)\n"
                                    f"You may retry with run_pipeline."
                                ),
                            )
                            runner.pipeline_execution_manager.remove_completion_subscribers(exe.id)
                            runner.completion_registry.cleanup(exe.id)
                    logger.info(
                        f"Notified subscribers of {len(interrupted)} interrupted pipeline(s)",
                    )
                except Exception as e:
                    logger.warning(f"Failed to wake subscribers of interrupted pipelines: {e}")
            if tracker:
                tracker.complete("Pipeline recovery")
        except Exception as e:
            logger.warning(f"Pipeline recovery after restart failed: {e}")
            if tracker:
                tracker.error("Pipeline recovery", str(e))

    # Start WebSocket server
    if runner.websocket_server:
        runner._websocket_task = asyncio.create_task(runner.websocket_server.start())
        if tracker:
            tracker.schedule("WebSocket server")

    # Auto-start UI dev server if configured
    if runner.config.ui.enabled and runner.config.ui.mode == "dev":
        from gobby.cli.utils import find_web_dir, spawn_ui_server

        web_dir = find_web_dir(runner.config)
        if web_dir:
            ui_log = Path(runner.config.telemetry.log_file).expanduser().parent / "ui.log"
            ui_host = runner.config.ui.host
            if runner.config.bind_host != "localhost" and ui_host == "localhost":
                ui_host = runner.config.bind_host
            ui_pid = spawn_ui_server(
                ui_host,
                runner.config.ui.port,
                web_dir,
                ui_log,
                daemon_port=runner.config.daemon_port,
                ws_port=runner.config.websocket.port if runner.config.websocket else 60888,
            )
            if ui_pid:
                logger.info(
                    f"UI dev server started (PID: {ui_pid}) "
                    f"at http://{ui_host}:{runner.config.ui.port}"
                )
            else:
                logger.warning("Failed to start UI dev server")
        else:
            logger.warning("UI dev mode enabled but web/ directory not found")

    if tracker:
        tracker.finish()
    logger.info("Subsystem initialization complete")


def _start_periodic_tasks(runner: GobbyRunner, **loops: Any) -> None:
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

    runner._approval_timeout_task = None
    if runner.pipeline_execution_manager:
        runner._approval_timeout_task = asyncio.create_task(
            loops["expire_approval_timeouts_loop"](
                runner.pipeline_execution_manager, lambda: runner._shutdown_requested
            ),
            name="approval-timeout-expiry",
        )

    # Count how many periodic tasks we started
    task_count = sum(
        1
        for t in (
            runner._metrics_cleanup_task,
            runner._metrics_archive_task,
            runner._span_cleanup_task,
            getattr(runner, "_memory_reconcile_task", None),
            runner._zombie_messages_task,
            runner._comms_messages_task,
            runner._expired_isolation_task,
            runner._metric_snapshot_task,
            runner._hook_inbox_task,
            runner._approval_timeout_task,
        )
        if t is not None
    )
    tracker = _startup_tracker
    if tracker:
        tracker.schedule(f"Periodic maintenance ({task_count} tasks)")


async def run_daemon(runner: GobbyRunner) -> None:
    """Main daemon startup, event loop, and shutdown sequence."""
    from gobby.runner_maintenance import (
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
        # Initialize startup tracker for CLI progress polling
        global _startup_tracker
        _startup_tracker = StartupTracker()

        setup_signal_handlers(lambda: setattr(runner, "_shutdown_requested", True))

        # Write PID file (ensures it exists regardless of how the runner
        # was started — CLI `gobby start`, launchctl, or direct invocation)
        from gobby.cli.utils import get_gobby_home

        pid_file = get_gobby_home() / "gobby.pid"
        try:
            pid_file.write_text(str(os.getpid()))
            logger.info(f"Wrote PID file: {pid_file} (PID {os.getpid()})")
        except OSError as e:
            logger.warning(f"Could not write PID file {pid_file}: {e}")

        try:
            if runner.agent_lifecycle_monitor:
                await runner.agent_lifecycle_monitor.cleanup_stale_pending_runs()
            rehydrated_runs = await _recover_agent_runs_after_restart(runner)
            if rehydrated_runs > 0:
                logger.info(
                    "Rehydrated completion events for %d active agent run(s)",
                    rehydrated_runs,
                )
        except Exception as e:
            logger.warning(f"Agent completion rehydration after restart failed: {e}")

        # Bind HTTP server immediately so health checks pass during init.
        # Allow in-flight HTTP requests a short drain period during shutdown.
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

        # Run all heavy initialization in background so HTTP stays responsive
        runner._subsystem_init_task = asyncio.create_task(
            _init_subsystems(runner, rebuild_vector_store),
            name="subsystem-init",
        )

        # Start periodic background tasks (lightweight, no blocking I/O)
        _start_periodic_tasks(
            runner,
            metrics_cleanup_loop=metrics_cleanup_loop,
            metrics_archive_loop=metrics_archive_loop,
            span_cleanup_loop=span_cleanup_loop,
            memory_reconcile_loop=memory_reconcile_loop,
            cleanup_zombie_messages_loop=cleanup_zombie_messages_loop,
            cleanup_comms_messages_loop=cleanup_comms_messages_loop,
            cleanup_expired_isolation_loop=cleanup_expired_isolation_loop,
            metric_snapshot_loop=metric_snapshot_loop,
            drain_hook_inbox_loop=drain_hook_inbox_loop,
            expire_approval_timeouts_loop=expire_approval_timeouts_loop,
        )

        # Wait for shutdown
        while not runner._shutdown_requested:
            await asyncio.sleep(0.5)

        # Cleanup with timeouts to prevent hanging
        # Use timeout slightly longer than uvicorn's graceful shutdown to let it finish
        await _await_critical_stop_hook_grace_window()
        logger.debug("Shutdown requested; beginning graceful shutdown")
        server.should_exit = True
        try:
            await runner.http_server._terminate_streamable_http_sessions()
        except Exception as e:
            logger.warning(f"Failed to terminate Streamable HTTP sessions: {e}")

        if (
            hasattr(runner, "_subsystem_init_task")
            and runner._subsystem_init_task
            and not runner._subsystem_init_task.done()
        ):
            logger.debug("Cancelling subsystem initialization during shutdown")
            runner._subsystem_init_task.cancel()
            try:
                await asyncio.wait_for(runner._subsystem_init_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        await _shutdown_websocket_server(runner)

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

        if runner.agent_lifecycle_monitor:
            try:
                cancelled_runs = await _cancel_active_agent_runs_for_shutdown(runner)
                if cancelled_runs > 0:
                    logger.info(
                        "Cancelled %d active agent run(s) during graceful shutdown",
                        cancelled_runs,
                    )
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

        # Cancel background pipeline tasks
        try:
            from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
                cleanup_background_tasks,
            )

            await asyncio.wait_for(cleanup_background_tasks(), timeout=5.0)
        except TimeoutError:
            logger.warning("Pipeline background tasks cleanup timed out")
        except Exception as e:
            logger.warning(f"Pipeline background tasks cleanup failed: {e}")

        # Cancel metrics cleanup task
        if runner._metrics_cleanup_task and not runner._metrics_cleanup_task.done():
            runner._metrics_cleanup_task.cancel()
            try:
                await asyncio.wait_for(runner._metrics_cleanup_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel metrics archive task
        if runner._metrics_archive_task and not runner._metrics_archive_task.done():
            runner._metrics_archive_task.cancel()
            try:
                await asyncio.wait_for(runner._metrics_archive_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel span cleanup task
        if runner._span_cleanup_task and not runner._span_cleanup_task.done():
            runner._span_cleanup_task.cancel()
            try:
                await asyncio.wait_for(runner._span_cleanup_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel metric snapshot task
        if runner._metric_snapshot_task and not runner._metric_snapshot_task.done():
            runner._metric_snapshot_task.cancel()
            try:
                await asyncio.wait_for(runner._metric_snapshot_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel hook inbox drain task
        if runner._hook_inbox_task and not runner._hook_inbox_task.done():
            runner._hook_inbox_task.cancel()
            try:
                await asyncio.wait_for(runner._hook_inbox_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel code index maintenance task
        if hasattr(runner, "_code_index_shutdown") and runner._code_index_shutdown:
            runner._code_index_shutdown.set()
        if (
            hasattr(runner, "_code_index_task")
            and runner._code_index_task
            and not runner._code_index_task.done()
        ):
            runner._code_index_task.cancel()
            try:
                await asyncio.wait_for(runner._code_index_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel code index sync worker
        if hasattr(runner, "_sync_worker_shutdown") and runner._sync_worker_shutdown:
            runner._sync_worker_shutdown.set()
        if (
            hasattr(runner, "_sync_worker_task")
            and runner._sync_worker_task
            and not runner._sync_worker_task.done()
        ):
            runner._sync_worker_task.cancel()
            try:
                await asyncio.wait_for(runner._sync_worker_task, timeout=5.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel zombie message cleanup task
        if runner._zombie_messages_task and not runner._zombie_messages_task.done():
            runner._zombie_messages_task.cancel()
            try:
                await asyncio.wait_for(runner._zombie_messages_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel comms message cleanup task
        if runner._comms_messages_task and not runner._comms_messages_task.done():
            runner._comms_messages_task.cancel()
            try:
                await asyncio.wait_for(runner._comms_messages_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel expired isolation cleanup task
        if runner._expired_isolation_task and not runner._expired_isolation_task.done():
            runner._expired_isolation_task.cancel()
            try:
                await asyncio.wait_for(runner._expired_isolation_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel vector store rebuild task
        if runner._vector_rebuild_task and not runner._vector_rebuild_task.done():
            runner._vector_rebuild_task.cancel()
            try:
                await asyncio.wait_for(runner._vector_rebuild_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Cancel memory reconciliation task
        if (
            hasattr(runner, "_memory_reconcile_task")
            and runner._memory_reconcile_task
            and not runner._memory_reconcile_task.done()
        ):
            runner._memory_reconcile_task.cancel()
            try:
                await asyncio.wait_for(runner._memory_reconcile_task, timeout=2.0)
            except (asyncio.CancelledError, TimeoutError):
                pass

        # Stop UI dev server if we started it
        if runner.config.ui.enabled and runner.config.ui.mode == "dev":
            from gobby.cli.utils import stop_ui_server

            stop_ui_server(quiet=True)

        # Close HookManager (webhook dispatcher httpx client, health monitor)
        hook_manager = getattr(runner.http_server, "_hook_manager", None)
        if hook_manager:
            try:
                hook_manager.shutdown()
            except Exception as e:
                logger.warning(f"HookManager shutdown failed: {e}")

        # Close MemoryManager (Neo4j httpx client)
        if runner.memory_manager:
            try:
                await asyncio.wait_for(runner.memory_manager.close(), timeout=5.0)
            except TimeoutError:
                logger.warning("MemoryManager close timed out")
            except Exception as e:
                logger.warning(f"MemoryManager close failed: {e}")

        # Close VectorStore connection
        if runner.vector_store:
            try:
                await asyncio.wait_for(runner.vector_store.close(), timeout=5.0)
            except TimeoutError:
                logger.warning("VectorStore close timed out")
            except Exception as e:
                logger.warning(f"VectorStore close failed: {e}")

        # NOTE: Shutdown JSONL exports removed to avoid git noise (#10198).
        # The pre-commit hook exports and stages JSONL files at commit time.

        try:
            await asyncio.wait_for(runner.mcp_proxy.disconnect_all(), timeout=3.0)
        except TimeoutError:
            logger.warning("MCP disconnect timed out")

        await _reap_remaining_child_processes()

        try:
            shutdown_telemetry()
        except Exception as e:
            logger.warning(f"Telemetry shutdown failed: {e}")

        try:
            runner.database.close()
        except Exception as e:
            logger.warning(f"Database close failed: {e}")

        # Clean up PID file on graceful shutdown
        cleanup_pid_file()

        logger.info("Shutdown complete")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        cleanup_pid_file()
        sys.exit(1)
