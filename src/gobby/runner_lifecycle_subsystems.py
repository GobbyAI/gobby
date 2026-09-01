"""Progressive subsystem initialization for the daemon lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.config.bootstrap import DEFAULT_WEBSOCKET_PORT
from gobby.config.logging import UI_LOG_FILENAME, resolved_log_path
from gobby.hooks.background_tasks import create_background_task
from gobby.runner_lifecycle_agents import (
    _reap_orphaned_srt_runners_on_startup,
    _recover_agent_completion_subscribers_on_startup,
    _retry_parked_non_task_resumes,
    _run_agent_hook_replay_barrier,
)
from gobby.runner_lifecycle_reconcile import (
    _reclassify_reconciliation_pending_runs,
    _reconcile_agent_runs_after_restart,
)
from gobby.runner_lifecycle_startup import StartupTracker

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger("gobby.runner_lifecycle")

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
) -> tuple[list[tuple[str, list[str] | None]], list[str]]:
    from gobby.storage.project_checkouts import LocalProjectCheckoutManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.utils.machine_id import require_machine_id

    project_manager = LocalProjectManager(database)
    checkout_manager = LocalProjectCheckoutManager(database)
    checkouts = {
        checkout.project_id: checkout
        for checkout in checkout_manager.list_for_machine(require_machine_id())
    }
    scopes: list[tuple[str, list[str] | None]] = []
    stale_project_ids: list[str] = []
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
            checkout = checkouts.get(project.id)
            if checkout is None or not Path(checkout.root_path).is_dir():
                stale_project_ids.append(project.id)
                continue
            scopes.append((project.id, None))
        offset += len(projects)
        if len(projects) < _PROJECT_ENUMERATION_PAGE_SIZE:
            break
    return scopes, stale_project_ids


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
        logger.error("MCP connection failed: %s", e)
        if tracker:
            tracker.error("MCP servers", str(e))


async def _repair_code_index_bm25(
    runner: GobbyRunner,
    tracker: StartupTracker | None,
) -> bool:
    """Verify and repair required BM25 indexes before code-index workers start."""
    config = runner.config_runtime.capture().snapshot.active
    if not config.code_index.enabled:
        return True

    from gobby.code_index.bm25_health import (
        repair_bm25_indexes,
        unavailable_bm25_status,
    )
    from gobby.runner_init.services import mark_service_degraded

    database_url = runner.startup_config.database_url
    if database_url:
        try:
            status = await asyncio.to_thread(
                repair_bm25_indexes,
                database_url,
                timeout_seconds=config.code_index.maintenance_index_timeout_seconds,
            )
        except Exception as exc:
            logger.exception("Unexpected code-index BM25 recovery failure")
            status = unavailable_bm25_status(str(exc))
    else:
        status = unavailable_bm25_status("PostgreSQL database_url is not configured")

    if status["healthy"]:
        repaired = [item["name"] for item in status["indexes"] if item["repaired"]]
        if repaired:
            logger.warning("Repaired damaged code-index BM25 indexes: %s", ", ".join(repaired))
        else:
            logger.info("Code-index BM25 indexes verified healthy")
        if tracker:
            tracker.complete("Code-index BM25 healthy")
        return True

    mark_service_degraded(runner, "code_index_bm25")
    failures = [
        f"{item['name']}: {item['error'] or item['state']}"
        for item in status["indexes"]
        if item["state"] != "healthy"
    ]
    detail = "; ".join(failures)
    logger.error(
        "Code-index BM25 recovery failed; maintenance and sync workers will not start: %s. "
        "Run `gobby postgres repair-code-index`, then restart Gobby.",
        detail,
    )
    if tracker:
        tracker.error("Code-index BM25", detail)
    return False


async def _check_embedding_service(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    config = runner.config_runtime.capture().snapshot.active
    emb_cfg = config.embeddings
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
            "Embedding endpoint unreachable at %s (model: %s) — semantic search will fall back to keyword search",
            emb_cfg.api_base,
            emb_cfg.model,
        )
    if tracker:
        tracker.error("Embeddings", failure_reason)


async def _cleanup_metrics_on_startup(runner: GobbyRunner) -> None:
    try:
        deleted = await _run_db(runner, runner.metrics_manager.cleanup_old_metrics)
        if deleted > 0:
            logger.info("Startup metrics cleanup: removed %s old entries", deleted)
    except Exception as e:
        logger.warning("Metrics cleanup failed: %s", e)


async def _cleanup_stale_expansion_runs_on_startup(runner: GobbyRunner) -> int:
    services = getattr(getattr(runner, "http_server", None), "services", None)
    task_manager = getattr(services, "task_manager", None)
    if task_manager is None:
        return 0

    try:
        from gobby.storage.expansion_runs import LocalExpansionRunManager

        run_manager = LocalExpansionRunManager(task_manager.db)
        cleaned = int(await _run_db(runner, run_manager.cleanup_stale_runs))
        if cleaned > 0:
            logger.info("Startup expansion cleanup: failed %s stale runs", cleaned)
        return cleaned
    except Exception as e:
        logger.warning("Expansion run cleanup failed: %s", e)
        return 0


async def _initialize_vector_store(
    runner: GobbyRunner,
    rebuild_vector_store: Any,
    tracker: StartupTracker | None,
) -> None:
    config = runner.config_runtime.capture().snapshot.active
    if not runner.vector_store:
        return

    try:
        await runner.vector_store.initialize()
        from gobby.mcp_proxy.semantic_search import SemanticToolSearch

        # tool_embeddings is derived index data. Recreate only this collection on
        # mismatch; search/discovery and explicit embedding routes repopulate it lazily.
        await runner.vector_store.ensure_collection(
            SemanticToolSearch.TOOL_COLLECTION,
            config.embeddings.dim,
            recreate_on_mismatch=True,
        )
        qdrant_count = await runner.vector_store.count()
        if qdrant_count == 0 and runner.memory_manager:
            memory_manager = runner.memory_manager
            hub_memories = memory_manager.storage.list_memories(limit=10000)
            if hub_memories:
                embed_fn = memory_manager.embed_fn
                if embed_fn:
                    logger.info(
                        "Qdrant empty, scheduling background rebuild from %s hub memories...",
                        len(hub_memories),
                    )

                    def memory_dicts() -> list[dict[str, str]]:
                        current_memories = memory_manager.storage.list_memories(limit=10000)
                        return [
                            {"id": m.id, "content": m.content, "project_id": m.project_id}
                            for m in current_memories
                        ]

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
        logger.warning("VectorStore initialization failed; lazy retry will continue: %s", e)


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
        logger.exception("%s start failed: %s", subsystem, e)
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
        logger.exception("%s start failed: %s", subsystem, e)
        if tracker:
            tracker.error(subsystem, str(e))


async def _start_core_services(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
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
        logger.warning("tmux health check failed on startup: %s", e)
        if tracker:
            tracker.error("tmux", str(e))


async def _start_terminal_host(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    from gobby.runner_init.services import mark_service_degraded

    host = getattr(runner, "terminal_host_manager", None)
    if host is None:
        return
    try:
        await host.start()
        if not host.native_available:
            mark_service_degraded(runner, "gterm_host")
            if tracker:
                tracker.error("gterm_host", host.last_error or "unavailable")
            return
        epoch = host.host_epoch
        registry = getattr(runner, "terminal_runtime_registry", None)
        if epoch and registry is not None:
            for backend in ("native", "tmux"):
                try:
                    runtime = registry.resolve(backend)
                except Exception:
                    continue
                if hasattr(runtime, "_frame_host_epoch"):
                    runtime._frame_host_epoch = str(epoch)
        if tracker:
            tracker.complete("gterm host")
    except Exception as e:
        mark_service_degraded(runner, "gterm_host")
        logger.warning("gterm host start failed on startup: %s", e)
        if tracker:
            tracker.error("gterm_host", str(e))


async def _start_agent_lifecycle_monitor(
    runner: GobbyRunner,
    tracker: StartupTracker | None,
) -> None:
    monitor = runner.agent_lifecycle_monitor
    if not monitor:
        return

    startup_errors: list[str] = []
    try:
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
            # Fail closed: without the running monitor there is no serialized
            # reclassification owner, so fenced runs would never resolve.
            raise RuntimeError(
                "Agent lifecycle monitor failed to start; agent lifecycle "
                "startup cannot continue without its reconciliation owner"
            ) from e
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
        from gobby.wiki.owner_dispatch import prune_gateway
        from gobby.wiki.prune_job import register_wiki_prune_cron
        from gobby.wiki.scheduled_jobs import (
            WIKI_JOB_NAME_PREFIX,
            park_wiki_cron_jobs,
            register_wiki_cron_jobs_for_projects,
        )

        if not runner.config_runtime.capture().snapshot.active.wiki.enabled:
            parked = await _run_db(runner, park_wiki_cron_jobs, cron_storage)
            logger.info(
                "Wiki cron registration skipped: wiki.enabled is false; parked %s row(s)",
                parked,
            )
            if tracker:
                tracker.complete("Wiki cron handlers")
            return

        await _run_db(
            runner,
            register_wiki_prune_cron,
            cron_storage=cron_storage,
            cron_executor=executor,
            gateway=prune_gateway(),
            project_id=getattr(runner, "project_id", None),
        )

        project_scopes, stale_project_ids = await _run_db(
            runner,
            _discover_wiki_cron_project_scopes,
            runner.database,
        )
        if not project_scopes and not stale_project_ids:
            if tracker:
                tracker.error("Wiki cron handlers", "skipped: no registered projects")
            return

        for stale_project_id in stale_project_ids:
            deleted = await _run_db(
                runner,
                cron_storage.delete_system_jobs_by_project_and_name_prefix,
                stale_project_id,
                WIKI_JOB_NAME_PREFIX,
            )
            logger.info(
                "Deleted %s stale wiki cron job(s) for project %s",
                deleted,
                stale_project_id,
            )

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
        if tracker:
            tracker.complete("Wiki cron handlers")
    except Exception as e:
        logger.exception("Failed to register wiki cron handlers: %s", e)
        if tracker:
            tracker.error("Wiki cron handlers", str(e))


async def _start_system_automation_loop(
    runner: GobbyRunner,
    tracker: StartupTracker | None,
) -> None:
    automation_loop = getattr(runner, "system_automation_loop", None)
    await _start_tracked_service(automation_loop, "System automation loop", tracker)


def _start_code_index_tasks(runner: GobbyRunner, tracker: StartupTracker | None) -> None:
    config = runner.config_runtime.capture().snapshot.active
    runner._code_index_task = None
    if runner.code_indexer:
        from gobby.code_index.maintenance import code_index_maintenance_loop

        summarizer = None
        if config.code_index.symbol_summary.enabled:
            from gobby.code_index.summarizer import SymbolSummarizer

            try:
                if runner.text_generation_service is None:
                    logger.warning("Skipping SymbolSummarizer: text generation service unavailable")
                else:
                    summarizer = SymbolSummarizer(
                        runner.text_generation_service,
                        config.code_index,
                    )
            except Exception as e:
                logger.warning("Failed to create SymbolSummarizer: %s", e)

        shutdown_event = asyncio.Event()
        runner._code_index_shutdown = shutdown_event
        runner._code_index_task = asyncio.create_task(
            code_index_maintenance_loop(
                context=runner.code_indexer,
                shutdown_flag=shutdown_event,
                interval=config.code_index.maintenance_interval_seconds,
                summarizer=summarizer,
                symbol_summary_batch_size=config.code_index.symbol_summary.batch_size,
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
                config=config.code_index,
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
        logger.warning("Pipeline recovery after restart failed: %s", e)
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
        logger.warning("Failed to wake subscribers of interrupted pipelines: %s", e)
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


def _schedule_workflow_skill_prewarm(runner: GobbyRunner) -> None:
    server = getattr(runner, "http_server", None)
    services = getattr(server, "services", None)
    hook_manager = getattr(server, "_hook_manager", None)
    handler = getattr(hook_manager, "_workflow_handler", None)
    engine = getattr(handler, "rule_engine", None)
    if services is None or engine is None:
        logger.debug(
            "Skipping workflow skill prewarm: services=%s rule_engine=%s",
            services is not None,
            engine is not None,
        )
        return

    task = create_background_task(engine.prewarm_skill_scripts(project_id=services.project_id))

    def report(completed: asyncio.Task[None]) -> None:
        if completed.cancelled():
            return
        exception = completed.exception()
        if exception is not None:
            logger.warning(
                "Workflow skill prewarm failed for project %s",
                services.project_id,
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    task.add_done_callback(report)


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
    config = runner.startup_config
    if not config.ui.enabled:
        return

    from gobby.cli.ui_mode import resolve_ui_mode
    from gobby.cli.utils import spawn_ui_server

    ui_resolution = resolve_ui_mode(config)
    if ui_resolution.effective != "dev":
        return

    web_dir = ui_resolution.source_web_dir
    if web_dir:
        ui_log = resolved_log_path(config.logging, UI_LOG_FILENAME)
        ui_host = config.ui.host
        if config.bind_host != "localhost" and ui_host == "localhost":
            ui_host = config.bind_host
        ui_pid = spawn_ui_server(
            ui_host,
            config.ui.port,
            web_dir,
            ui_log,
            daemon_port=config.daemon_port,
            ws_port=config.websocket.port if config.websocket else DEFAULT_WEBSOCKET_PORT,
        )
        if ui_pid:
            logger.info(
                "UI dev server started (PID: %s) at http://%s:%s for daemon UI mode %s",
                ui_pid,
                ui_host,
                config.ui.port,
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
    reconcile_agent_runs_after_restart: AgentLifecycleOperation = (
        _reconcile_agent_runs_after_restart
    ),
    reap_orphaned_srt_runners: AgentLifecycleOperation = (_reap_orphaned_srt_runners_on_startup),
    recover_agent_completion_subscribers: AgentLifecycleOperation = (
        _recover_agent_completion_subscribers_on_startup
    ),
) -> None:
    """Heavy initialization that runs after HTTP is already serving."""
    monitor = getattr(runner, "agent_lifecycle_monitor", None)
    if monitor is None:
        if getattr(runner, "agent_runner", None) is not None:
            raise RuntimeError("Agent reconciliation owner is unavailable")
    else:
        monitor.set_reconciliation_callback(lambda: _reclassify_reconciliation_pending_runs(runner))
        monitor.set_non_task_resume_callback(lambda: _retry_parked_non_task_resumes(runner))
    await _run_agent_hook_replay_barrier(runner)
    reconciled_runs = (
        await reconcile_agent_runs_after_restart(runner)
        if getattr(runner, "agent_runner", None) is not None
        else 0
    )
    if reconciled_runs > 0:
        logger.info(
            "Reconciled %d active agent run(s) after daemon restart",
            reconciled_runs,
        )
    try:
        await reap_orphaned_srt_runners(runner)
    except Exception:
        logger.exception("SRT sandbox runner cleanup failed during startup")
    try:
        recovered_subscribers = await recover_agent_completion_subscribers(runner)
        if recovered_subscribers > 0:
            logger.info(
                "Recovered %d agent completion subscriber notification(s)",
                recovered_subscribers,
            )
    except Exception:
        logger.exception("Agent completion subscriber recovery failed during startup")
    await _connect_mcp_servers(runner, tracker)
    code_index_bm25_ready = await _repair_code_index_bm25(runner, tracker)
    await _check_embedding_service(runner, tracker)
    await _cleanup_metrics_on_startup(runner)
    await _cleanup_stale_expansion_runs_on_startup(runner)
    await _initialize_vector_store(runner, rebuild_vector_store, tracker)
    await _start_core_services(runner, tracker)
    await _check_tmux_health(tracker)
    await _start_terminal_host(runner, tracker)
    await _start_agent_lifecycle_monitor(
        runner,
        tracker,
    )
    await _start_cron_scheduler(runner, tracker)
    if code_index_bm25_ready:
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
    _schedule_workflow_skill_prewarm(runner)
    if tracker:
        tracker.finish()
    if services is not None:
        services.startup_ready = True
    logger.info("Subsystem initialization complete")
