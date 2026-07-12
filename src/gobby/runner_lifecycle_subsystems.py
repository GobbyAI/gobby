"""Progressive subsystem initialization for the daemon lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.config.bootstrap import DEFAULT_WEBSOCKET_PORT
from gobby.config.persistence import is_falkordb_enabled
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

_PROJECT_ENUMERATION_PAGE_SIZE = 100
_PIPELINE_EXECUTION_PAGE_SIZE = 100


async def _run_db(
    runner: GobbyRunner,
    operation: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    db_executor = getattr(runner, "db_executor", None)
    if db_executor is not None:
        return await db_executor.run(operation, *args, **kwargs)
    return await asyncio.to_thread(operation, *args, **kwargs)


def _discover_wiki_cron_project_scopes(
    database: Any,
) -> tuple[list[tuple[str, list[str] | None]], list[tuple[str, str]]]:
    from gobby.storage.projects import LocalProjectManager

    project_manager = LocalProjectManager(database)
    scopes: list[tuple[str, list[str] | None]] = []
    errors: list[tuple[str, str]] = []
    offset = 0
    while True:
        projects = project_manager.list_page(
            limit=_PROJECT_ENUMERATION_PAGE_SIZE,
            offset=offset,
        )
        if not projects:
            break
        for project in projects:
            if project_manager.is_protected(project):
                continue
            if not project.repo_path:
                errors.append((project.id, "skipped: project has no repo path"))
                continue
            if not Path(project.repo_path).exists():
                errors.append(
                    (
                        project.id,
                        f"skipped: project repo path does not exist: {project.repo_path}",
                    )
                )
                continue
            scopes.append((project.id, None))
        offset += len(projects)
        if len(projects) < _PROJECT_ENUMERATION_PAGE_SIZE:
            break
    return scopes, errors


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

    if runner.memory_manager and is_falkordb_enabled(db_cfg):
        falkor_cfg = db_cfg.falkordb
        falkor_client = getattr(runner.memory_manager, "falkor_client", None)
        is_healthy = bool(falkor_client and await falkor_client.ping())
        endpoint = f"{falkor_cfg.host}:{falkor_cfg.port}"
        if not is_healthy:
            logger.warning(
                "FalkorDB configured but unreachable at %s — memory graph features disabled",
                endpoint,
            )
            runner.memory_manager.clear_graph_clients()
            if tracker:
                tracker.error("FalkorDB", f"unreachable at {endpoint}")
        elif tracker:
            tracker.complete("FalkorDB healthy")
    elif runner.memory_manager:
        logger.debug("FalkorDB is not configured; graph health check skipped")


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
            f"(model: {emb_cfg.model}) — semantic search will fall back to keyword search"
        )
    if tracker:
        tracker.error("Embeddings", failure_reason)


async def _cleanup_metrics_on_startup(runner: GobbyRunner) -> None:
    try:
        db_executor = getattr(runner, "db_executor", None)
        run_db = getattr(db_executor, "run", None)
        if run_db is None:
            deleted = await asyncio.to_thread(runner.metrics_manager.cleanup_old_metrics)
        else:
            deleted = await run_db(runner.metrics_manager.cleanup_old_metrics)
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

        # tool_embeddings is derived index data. Recreate only this collection on
        # mismatch; search/discovery and explicit embedding routes repopulate it lazily.
        await runner.vector_store.ensure_collection(
            SemanticToolSearch.TOOL_COLLECTION,
            runner.config.embeddings.dim,
            recreate_on_mismatch=True,
        )
        qdrant_count = await runner.vector_store.count()
        if qdrant_count == 0 and runner.memory_manager:
            hub_memories = runner.memory_manager.storage.list_memories(limit=10000)
            if hub_memories:
                embed_fn = runner.memory_manager.embed_fn
                if embed_fn:
                    logger.info(
                        f"Qdrant empty, scheduling background rebuild from "
                        f"{len(hub_memories)} hub memories..."
                    )
                    memory_dicts = [{"id": m.id, "content": m.content} for m in hub_memories]
                    runner._vector_rebuild_task = asyncio.create_task(
                        rebuild_vector_store(runner.vector_store, memory_dicts, embed_fn),
                        name="vector-store-rebuild",
                    )
                    if tracker:
                        tracker.schedule(f"Vector store rebuild ({len(hub_memories)} memories)")
                else:
                    logger.warning("No embed_fn configured, skipping VectorStore rebuild")
        if tracker:
            tracker.complete("Vector store initialized")
    except Exception as e:
        logger.warning(f"VectorStore initialization failed; lazy retry will continue: {e}")


async def _start_tracked_service(
    service: Any,
    subsystem: str,
    tracker: StartupTracker | None,
) -> None:
    if service is None:
        return
    try:
        await service.start()
    except Exception as e:
        logger.error("%s start failed: %s", subsystem, e, exc_info=True)
        if tracker:
            tracker.error(subsystem, str(e))
        return
    if tracker:
        tracker.complete(subsystem)


def _run_tracked_start(
    operation: Callable[[], None],
    subsystem: str,
    tracker: StartupTracker | None,
) -> None:
    try:
        operation()
    except Exception as e:
        logger.error("%s start failed: %s", subsystem, e, exc_info=True)
        if tracker:
            tracker.error(subsystem, str(e))


async def _start_core_services(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    await _start_tracked_service(runner.message_processor, "Message processor", tracker)
    await _start_tracked_service(
        runner.communications_manager,
        "Communications manager",
        tracker,
    )
    await _start_tracked_service(
        runner.lifecycle_manager,
        "Session lifecycle manager",
        tracker,
    )


async def _check_tmux_health(tracker: StartupTracker | None) -> None:
    try:
        from gobby.agents.tmux import get_tmux_session_manager

        tmux_mgr = get_tmux_session_manager()
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
    await _register_wiki_cron_handlers(runner, tracker)
    await _start_tracked_service(runner.cron_scheduler, "Cron scheduler", tracker)


async def _register_wiki_cron_handlers(
    runner: GobbyRunner,
    tracker: StartupTracker | None,
) -> None:
    cron_storage = getattr(runner, "cron_storage", None)
    if cron_storage is None:
        if tracker:
            tracker.error("Wiki cron handlers", "skipped: cron storage unavailable")
        return
    executor = getattr(runner.cron_scheduler, "executor", None)
    if executor is None:
        if tracker:
            tracker.error("Wiki cron handlers", "skipped: cron executor unavailable")
        return
    try:
        from gobby.wiki.scheduled_jobs import (
            register_wiki_cron_jobs_for_projects,
        )

        project_scopes, project_errors = await _run_db(
            runner,
            _discover_wiki_cron_project_scopes,
            runner.database,
        )
        if not project_scopes and not project_errors:
            if tracker:
                tracker.error("Wiki cron handlers", "skipped: no registered projects")
            return
        if tracker:
            for project_id, error in project_errors:
                tracker.error(f"Wiki cron handlers ({project_id})", error)

        registered = await register_wiki_cron_jobs_for_projects(
            cron_storage=cron_storage,
            cron_executor=executor,
            db=runner.database,
            project_scopes=project_scopes,
            run_sync=lambda operation, *args, **kwargs: _run_db(
                runner,
                operation,
                *args,
                **kwargs,
            ),
        )
        logger.debug("Wiki cron handlers registered: %s", registered)
        if not project_scopes and registered == 0 and tracker:
            tracker.error("Wiki cron handlers", "skipped: no wiki-capable projects")
        elif tracker:
            tracker.complete("Wiki cron handlers")
    except Exception as e:
        logger.error("Failed to register wiki cron handlers: %s", e, exc_info=True)
        if tracker:
            tracker.error("Wiki cron handlers", str(e))


async def _start_system_automation_loop(
    runner: GobbyRunner,
    tracker: StartupTracker | None,
) -> None:
    automation_loop = getattr(runner, "system_automation_loop", None)
    await _start_tracked_service(automation_loop, "System automation loop", tracker)


def _start_code_index_tasks(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    runner._code_index_task = None
    if runner.code_indexer:
        from gobby.code_index.maintenance import code_index_maintenance_loop

        summarizer = None
        if runner.config.code_index.symbol_summary.enabled:
            from gobby.code_index.summarizer import SymbolSummarizer

            try:
                if runner.text_generation_service is None:
                    logger.warning("Skipping SymbolSummarizer: text generation service unavailable")
                else:
                    summarizer = SymbolSummarizer(
                        runner.text_generation_service,
                        runner.config.code_index,
                    )
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
                symbol_summary_batch_size=runner.config.code_index.symbol_summary.batch_size,
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
                context=runner.code_indexer,
                config=runner.config.code_index,
                shutdown_flag=sync_shutdown,
                run_db=runner.code_indexer.run_db,
            ),
            name="code-index-sync-worker",
        )
        if tracker:
            tracker.schedule("Code index sync")


async def _recover_pipelines(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    if runner.workflow_loader is None:
        if tracker:
            tracker.error("Pipeline recovery", "skipped: workflow loader unavailable")
        return

    try:
        from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
            resume_interrupted_pipelines,
        )
        from gobby.runner_pipeline_runtime import build_pipeline_runtime
        from gobby.storage.pipelines import LocalPipelineExecutionManager

        discovery_manager = LocalPipelineExecutionManager(runner.database, project_id=None)
        after_project_id: str | None = None
        failed_projects = 0
        while True:
            project_ids = await _run_db(
                runner,
                discovery_manager.list_recovery_project_ids,
                limit=_PROJECT_ENUMERATION_PAGE_SIZE,
                after_project_id=after_project_id,
            )
            if not project_ids:
                break

            for project_id in project_ids:
                if bool(getattr(runner, "_shutdown_requested", False)):
                    if tracker:
                        tracker.error("Pipeline recovery", "stopped: daemon shutdown requested")
                    return
                try:
                    if (
                        runner.project_id == project_id
                        and runner.pipeline_execution_manager is not None
                        and runner.pipeline_executor is not None
                    ):
                        execution_manager = runner.pipeline_execution_manager
                        executor = runner.pipeline_executor
                    else:
                        execution_manager, executor = build_pipeline_runtime(runner, project_id)

                    resumed_ids = await resume_interrupted_pipelines(
                        loader=runner.workflow_loader,
                        executor=executor,
                        execution_manager=execution_manager,
                        project_id=project_id,
                        run_db=lambda operation, *args, **kwargs: _run_db(
                            runner,
                            operation,
                            *args,
                            **kwargs,
                        ),
                    )
                    if resumed_ids:
                        logger.info(
                            "Resumed %d pipeline(s) for project %s after restart: %s",
                            len(resumed_ids),
                            project_id,
                            resumed_ids,
                        )

                    stale_count = await _run_db(
                        runner,
                        execution_manager.interrupt_stale_running_executions,
                        exclude_ids=set(resumed_ids),
                    )
                    if stale_count > 0:
                        logger.info(
                            "Interrupted %d non-resumable stale pipeline execution(s) "
                            "for project %s",
                            stale_count,
                            project_id,
                        )

                    if runner.completion_registry is not None:
                        await _wake_interrupted_pipeline_subscribers(
                            runner,
                            execution_manager,
                            runner.completion_registry,
                        )
                except Exception as e:
                    failed_projects += 1
                    logger.warning("Pipeline recovery failed for project %s: %s", project_id, e)
                    if tracker:
                        tracker.error(f"Pipeline recovery ({project_id})", str(e))

            after_project_id = project_ids[-1]
            if len(project_ids) < _PROJECT_ENUMERATION_PAGE_SIZE:
                break

        if runner.completion_registry is None and tracker:
            tracker.error(
                "Pipeline recovery", "subscriber notifications skipped: registry unavailable"
            )
        elif failed_projects == 0 and tracker:
            tracker.complete("Pipeline recovery")
    except Exception as e:
        logger.warning(f"Pipeline recovery after restart failed: {e}")
        if tracker:
            tracker.error("Pipeline recovery", str(e))


async def _wake_interrupted_pipeline_subscribers(
    runner: GobbyRunner,
    execution_manager: Any,
    completion_registry: Any,
) -> int:
    try:
        from gobby.workflows.pipeline_state import ExecutionStatus as _ES

        notified = 0
        offset = 0
        while True:
            interrupted = await _run_db(
                runner,
                execution_manager.list_executions,
                status=_ES.INTERRUPTED,
                limit=_PIPELINE_EXECUTION_PAGE_SIZE,
                offset=offset,
            )
            for exe in interrupted:
                subs = await _run_db(
                    runner,
                    execution_manager.get_completion_subscribers,
                    exe.id,
                )
                if not subs:
                    continue
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
                await _run_db(
                    runner,
                    execution_manager.remove_completion_subscribers,
                    exe.id,
                )
                completion_registry.cleanup(exe.id)
                notified += 1
            offset += len(interrupted)
            if len(interrupted) < _PIPELINE_EXECUTION_PAGE_SIZE:
                break
        logger.info("Notified subscribers of %d interrupted pipeline(s)", notified)
        return notified
    except Exception as e:
        logger.warning(f"Failed to wake subscribers of interrupted pipelines: {e}")
        raise


def _record_websocket_startup_result(
    task: asyncio.Future[None], tracker: StartupTracker | None
) -> None:
    """Report background WebSocket startup failures as soon as they happen."""
    if task.cancelled():
        return

    error = task.exception()
    if error is None:
        return

    logger.error(
        "WebSocket server startup failed",
        exc_info=(type(error), error, error.__traceback__),
    )
    if tracker:
        tracker.error("WebSocket server", str(error))


def _start_websocket_server(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    if runner.websocket_server:
        runner._websocket_task = asyncio.create_task(
            runner.websocket_server.start(), name="websocket-server"
        )
        runner._websocket_task.add_done_callback(
            lambda task: _record_websocket_startup_result(task, tracker)
        )
        if tracker:
            tracker.schedule("WebSocket server")


def _maybe_start_ui_dev_server(runner: GobbyRunner) -> None:
    if not runner.config.ui.enabled:
        return

    from gobby.cli.ui_mode import resolve_ui_mode
    from gobby.cli.utils import spawn_ui_server

    ui_resolution = resolve_ui_mode(runner.config)
    if ui_resolution.effective != "dev":
        return

    web_dir = ui_resolution.source_web_dir
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
            ws_port=runner.config.websocket.port
            if runner.config.websocket
            else DEFAULT_WEBSOCKET_PORT,
        )
        if ui_pid:
            logger.info(
                "UI dev server started (PID: %s) at http://%s:%s for daemon UI mode %s",
                ui_pid,
                ui_host,
                runner.config.ui.port,
                ui_resolution.display,
            )
        else:
            logger.warning("Failed to start UI dev server")
    else:
        logger.warning("UI dev mode effective but source web/ directory not found")


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
    await _cleanup_metrics_on_startup(runner)
    await _initialize_vector_store(runner, rebuild_vector_store, tracker)
    await _start_core_services(runner, tracker)
    await _check_tmux_health(tracker)
    await _start_agent_lifecycle_monitor(
        runner,
        tracker,
        reconcile_agent_runs_after_restart,
    )
    await _start_cron_scheduler(runner, tracker)
    _run_tracked_start(
        lambda: _start_code_index_tasks(runner, tracker),
        "Code index tasks",
        tracker,
    )
    await _recover_pipelines(runner, tracker)
    services = getattr(getattr(runner, "http_server", None), "services", None)
    if services is not None and bool(getattr(services, "shutdown_in_progress", False)):
        logger.info("Subsystem initialization stopped because daemon shutdown is in progress")
        return

    _run_tracked_start(
        lambda: _start_websocket_server(runner, tracker),
        "WebSocket server",
        tracker,
    )
    _run_tracked_start(
        lambda: _maybe_start_ui_dev_server(runner),
        "UI development server",
        tracker,
    )
    await _start_system_automation_loop(runner, tracker)
    if services is not None and bool(getattr(services, "shutdown_in_progress", False)):
        logger.info("Subsystem initialization stopped because daemon shutdown is in progress")
        return
    if tracker:
        tracker.finish()
    if services is not None:
        services.startup_ready = True
    logger.info("Subsystem initialization complete")
