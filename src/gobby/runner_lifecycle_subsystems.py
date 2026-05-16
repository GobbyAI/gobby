"""Progressive subsystem initialization for the daemon lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.runner_lifecycle_agents import (
    _reconcile_agent_runs_after_restart,
)
from gobby.runner_lifecycle_startup import (
    StartupTracker,
    _record_provider_model_refresh_result,
    _refresh_provider_model_catalog,
)

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")

ProviderCatalogRefresh = Callable[[Any, Any], Coroutine[Any, Any, dict[str, dict[str, Any]]]]
ProviderCatalogRefreshRecorder = Callable[
    [asyncio.Future[dict[str, dict[str, Any]]], StartupTracker | None],
    None,
]
AgentLifecycleOperation = Callable[[Any], Awaitable[int]]


def _schedule_provider_model_refresh(
    runner: GobbyRunner,
    tracker: StartupTracker | None,
    refresh_provider_model_catalog: ProviderCatalogRefresh,
    record_provider_model_refresh_result: ProviderCatalogRefreshRecorder,
) -> None:
    provider_catalog = getattr(runner.http_server.services, "provider_model_catalog", None)
    if provider_catalog is None:
        return

    runner._provider_model_refresh_task = asyncio.create_task(
        refresh_provider_model_catalog(provider_catalog, runner.http_server.codex_client),
        name="provider-model-catalog-refresh",
    )
    runner._provider_model_refresh_task.add_done_callback(
        lambda task: record_provider_model_refresh_result(task, tracker)
    )
    if tracker:
        tracker.schedule("Provider model catalog refresh")


async def _connect_mcp_servers(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    try:
        await asyncio.wait_for(runner.mcp_proxy.connect_all(), timeout=10.0)
        if tracker:
            tracker.complete("MCP servers connected")
    except TimeoutError:
        logger.error("MCP connection timed out")
        if tracker:
            tracker.error("MCP servers", "connection timed out")
    except Exception as e:
        logger.error(f"MCP connection failed: {e}")
        if tracker:
            tracker.error("MCP servers", str(e))


async def _check_external_services(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    db_cfg = runner.config.databases
    if db_cfg.qdrant.url:
        from gobby.cli.services import is_qdrant_healthy

        if not await is_qdrant_healthy(db_cfg.qdrant.url):
            logger.warning(
                f"Qdrant configured but unreachable at {db_cfg.qdrant.url} — "
                "vector features will retry lazily"
            )
            if tracker:
                tracker.error("Qdrant", f"unreachable at {db_cfg.qdrant.url}")
        elif tracker:
            tracker.complete("Qdrant healthy")
    else:
        logger.debug("Qdrant URL is not configured; vector health check skipped")

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
    elif runner.memory_manager:
        logger.debug("Neo4j URL is not configured; graph health check skipped")


async def _check_embedding_service(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    emb_cfg = runner.config.embeddings
    if not emb_cfg.api_base:
        return

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
        return

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


def _cleanup_metrics_on_startup(runner: GobbyRunner) -> None:
    try:
        deleted = runner.metrics_manager.cleanup_old_metrics()
        if deleted > 0:
            logger.info(f"Startup metrics cleanup: removed {deleted} old entries")
    except Exception as e:
        logger.warning(f"Metrics cleanup failed: {e}")


async def _initialize_vector_store(
    runner: GobbyRunner,
    rebuild_vector_store: Any,
    tracker: StartupTracker | None,
) -> None:
    if not runner.vector_store:
        return

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
                        tracker.schedule(f"Vector store rebuild ({len(sqlite_memories)} memories)")
                else:
                    logger.warning("No embed_fn configured, skipping VectorStore rebuild")
        if tracker:
            tracker.complete("Vector store initialized")
    except Exception as e:
        logger.warning(f"VectorStore initialization failed; lazy retry will continue: {e}")


async def _start_core_services(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    if runner.message_processor:
        await runner.message_processor.start()
        if tracker:
            tracker.complete("Message processor")

    if runner.communications_manager:
        try:
            await runner.communications_manager.start()
            if tracker:
                tracker.complete("Communications manager")
        except Exception as e:
            logger.error(f"CommunicationsManager start failed: {e}")
            if tracker:
                tracker.error("Communications manager", str(e))

    await runner.lifecycle_manager.start()
    if tracker:
        tracker.complete("Session lifecycle manager")


async def _check_tmux_health(tracker: StartupTracker | None) -> None:
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


async def _start_agent_lifecycle_monitor(
    runner: GobbyRunner,
    tracker: StartupTracker | None,
    reconcile_agent_runs_after_restart: AgentLifecycleOperation,
) -> None:
    monitor = runner.agent_lifecycle_monitor
    if not monitor:
        return

    startup_errors: list[str] = []
    try:
        try:
            reconciled_runs = await reconcile_agent_runs_after_restart(runner)
            if reconciled_runs > 0:
                logger.info(
                    "Reconciled %d active agent run(s) after daemon restart",
                    reconciled_runs,
                )
        except Exception as e:
            startup_errors.append(f"reconcile failed: {e}")
            logger.exception("Agent restart reconciliation failed during startup")

        try:
            await monitor.cleanup_stale_pending_runs()
        except Exception as e:
            startup_errors.append(f"cleanup failed: {e}")
            logger.exception("Agent stale pending cleanup failed during startup")

        try:
            await monitor.start()
        except Exception as e:
            startup_errors.append(f"start failed: {e}")
            logger.exception("Agent lifecycle monitor start failed during startup")
    finally:
        if tracker:
            if startup_errors:
                tracker.error("Agent lifecycle monitor", "; ".join(startup_errors))
            else:
                tracker.complete("Agent lifecycle monitor")


async def _start_cron_scheduler(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    if runner.cron_scheduler:
        await runner.cron_scheduler.start()
        if tracker:
            tracker.complete("Cron scheduler")


def _start_code_index_tasks(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    runner._code_index_task = None
    if runner.code_indexer:
        from gobby.code_index.maintenance import code_index_maintenance_loop

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
                run_db=runner.code_indexer.run_db,
            ),
            name="code-index-sync-worker",
        )
        if tracker:
            tracker.schedule("Code index sync")


async def _recover_pipelines(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    if not (
        runner.pipeline_executor and runner.pipeline_execution_manager and runner.workflow_loader
    ):
        return

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

        if stale_count > 0 and runner.completion_registry:
            await _wake_interrupted_pipeline_subscribers(runner)
        if tracker:
            tracker.complete("Pipeline recovery")
    except Exception as e:
        logger.warning(f"Pipeline recovery after restart failed: {e}")
        if tracker:
            tracker.error("Pipeline recovery", str(e))


async def _wake_interrupted_pipeline_subscribers(runner: GobbyRunner) -> None:
    execution_manager = runner.pipeline_execution_manager
    completion_registry = runner.completion_registry
    if execution_manager is None or completion_registry is None:
        return

    try:
        from gobby.workflows.pipeline_state import ExecutionStatus as _ES

        interrupted = execution_manager.list_executions(
            status=_ES.INTERRUPTED,
        )
        for exe in interrupted:
            subs = execution_manager.get_completion_subscribers(exe.id)
            if subs:
                completion_registry.register(exe.id, subscribers=subs)
                await completion_registry.notify(
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
                execution_manager.remove_completion_subscribers(exe.id)
                completion_registry.cleanup(exe.id)
        logger.info(
            f"Notified subscribers of {len(interrupted)} interrupted pipeline(s)",
        )
    except Exception as e:
        logger.warning(f"Failed to wake subscribers of interrupted pipelines: {e}")


def _start_websocket_server(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    if runner.websocket_server:
        runner._websocket_task = asyncio.create_task(runner.websocket_server.start())
        if tracker:
            tracker.schedule("WebSocket server")


def _maybe_start_ui_dev_server(runner: GobbyRunner) -> None:
    if not (runner.config.ui.enabled and runner.config.ui.mode == "dev"):
        return

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
                f"UI dev server started (PID: {ui_pid}) at http://{ui_host}:{runner.config.ui.port}"
            )
        else:
            logger.warning("Failed to start UI dev server")
    else:
        logger.warning("UI dev mode enabled but web/ directory not found")


async def init_subsystems(
    runner: GobbyRunner,
    rebuild_vector_store: Any,
    tracker: StartupTracker | None,
    *,
    refresh_provider_model_catalog: ProviderCatalogRefresh = _refresh_provider_model_catalog,
    record_provider_model_refresh_result: ProviderCatalogRefreshRecorder = (
        _record_provider_model_refresh_result
    ),
    reconcile_agent_runs_after_restart: AgentLifecycleOperation = (
        _reconcile_agent_runs_after_restart
    ),
) -> None:
    """Heavy initialization that runs after HTTP is already serving."""
    _schedule_provider_model_refresh(
        runner,
        tracker,
        refresh_provider_model_catalog,
        record_provider_model_refresh_result,
    )
    await _connect_mcp_servers(runner, tracker)
    await _check_external_services(runner, tracker)
    await _check_embedding_service(runner, tracker)
    _cleanup_metrics_on_startup(runner)
    await _initialize_vector_store(runner, rebuild_vector_store, tracker)
    await _start_core_services(runner, tracker)
    await _check_tmux_health(tracker)
    await _start_agent_lifecycle_monitor(
        runner,
        tracker,
        reconcile_agent_runs_after_restart,
    )
    await _start_cron_scheduler(runner, tracker)
    _start_code_index_tasks(runner, tracker)
    await _recover_pipelines(runner, tracker)
    _start_websocket_server(runner, tracker)
    _maybe_start_ui_dev_server(runner)

    if tracker:
        tracker.finish()
    services = getattr(getattr(runner, "http_server", None), "services", None)
    if services is not None:
        services.startup_ready = True
        services.shutdown_in_progress = False
    logger.info("Subsystem initialization complete")
