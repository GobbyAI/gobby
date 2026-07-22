"""Workflow, agent, cron, and communication setup for GobbyRunner."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.runner import AgentRunner
from gobby.autonomous.progress_tracker import ProgressTracker
from gobby.autonomous.stuck_detector import StuckDetector
from gobby.runner_init.services import mark_service_degraded
from gobby.sessions.lifecycle import SessionLifecycleManager

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner
    from gobby.system_automation import PipelineHeartbeatService

logger = logging.getLogger(__name__)

RETIRED_SYSTEM_CRON_JOBS = ("gobby:conductor-tick", "gobby:pipeline-heartbeat")


class _CronDependencyUnavailable(Exception):
    """Stop cron setup after a required dependency already logged its failure."""


async def _send_tmux_session_wake(
    tmux_session_name: str,
    message: str,
    *,
    submit: bool = False,
    escape_before_submit: bool = False,
) -> None:
    from gobby.agents.tmux import get_tmux_session_manager
    from gobby.agents.tmux.text_injection import TMUX_TEXT_ENTER_DELAY_SECONDS

    mgr = get_tmux_session_manager()
    if not submit:
        if not await mgr.send_keys(tmux_session_name, message):
            raise RuntimeError(f"tmux send-keys to {tmux_session_name} failed")
        return

    literal_text = message.rstrip("\n")
    if escape_before_submit:
        if not await mgr.send_keys(tmux_session_name, "Escape", literal=False):
            raise RuntimeError(f"tmux send-keys to {tmux_session_name} failed")
        if literal_text:
            await asyncio.sleep(TMUX_TEXT_ENTER_DELAY_SECONDS)
    if literal_text and not await mgr.send_keys(tmux_session_name, literal_text):
        raise RuntimeError(f"tmux send-keys to {tmux_session_name} failed")
    if literal_text:
        await asyncio.sleep(TMUX_TEXT_ENTER_DELAY_SECONDS)
    if not await mgr.send_keys(tmux_session_name, "Enter", literal=False):
        raise RuntimeError(f"tmux send-keys to {tmux_session_name} failed")


async def _send_tmux_pane_wake(
    pane_id: str,
    message: str,
    tmux_socket_path: str | None,
    *,
    submit: bool = False,
    escape_before_submit: bool = False,
) -> None:
    from gobby.agents.tmux.text_injection import (
        send_literal_text_to_tmux_target,
        submit_literal_text_to_tmux_target,
    )

    tmux_cmd = ["tmux"]
    if tmux_socket_path:
        tmux_cmd.extend(["-S", tmux_socket_path])

    if submit:
        await submit_literal_text_to_tmux_target(
            pane_id,
            message,
            tmux_cmd=tmux_cmd,
            escape_before_submit=escape_before_submit,
        )
    else:
        await send_literal_text_to_tmux_target(pane_id, message, tmux_cmd=tmux_cmd)


def _init_pipeline_heartbeat(runner: GobbyRunner) -> PipelineHeartbeatService | None:
    """Create a cross-project pipeline heartbeat for the daemon."""
    try:
        from gobby.storage.agents import LocalAgentRunManager
        from gobby.storage.pipelines import LocalPipelineExecutionManager
        from gobby.workflows.pipeline_heartbeat import PipelineHeartbeat

        execution_manager = LocalPipelineExecutionManager(db=runner.database, project_id=None)
        heartbeat = PipelineHeartbeat(
            execution_manager=execution_manager,
            task_manager=runner.task_manager,
            agent_run_manager=LocalAgentRunManager(runner.database),
            session_manager=runner.session_manager,
            run_db=runner.db_executor.run,
        )
        if runner.project_id is None:
            logger.info(
                "Daemon has no startup project; pipeline heartbeat will monitor all projects"
            )
        else:
            logger.debug("Cross-project PipelineHeartbeat maintenance registered")
        return heartbeat
    except Exception:
        mark_service_degraded(runner, "pipeline_heartbeat")
        logger.exception("Failed to initialize pipeline heartbeat maintenance")
        return None


def init_orchestration(runner: GobbyRunner) -> None:
    """Initialize workflows, pipelines, agents, cron, and communications."""
    runner.workflow_loader = None
    runner.pipeline_execution_manager = None
    runner.pipeline_executor = None
    try:
        from gobby.workflows.loader import WorkflowLoader

        runner.workflow_loader = WorkflowLoader(db=runner.database)
    except Exception:
        mark_service_degraded(runner, "workflow_loader")
        logger.warning("Failed to initialize workflow loader", exc_info=True)

    from gobby.agents.tmux import configure_tmux
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.events.wake import WakeDispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.inter_session_messages import InterSessionMessageManager

    ism_manager = InterSessionMessageManager(runner.database)
    agent_run_manager = LocalAgentRunManager(runner.database)
    configure_tmux(runner.config.tmux)

    runner.wake_dispatcher = WakeDispatcher(
        session_manager=runner.session_manager,
        ism_manager=ism_manager,
        tmux_sender=_send_tmux_session_wake,
        tmux_pane_sender=_send_tmux_pane_wake,
        agent_run_manager=agent_run_manager,
    )

    runner.completion_registry = CompletionEventRegistry(
        wake_callback=runner.wake_dispatcher.wake,
    )

    if runner.workflow_loader is not None and runner.project_id:
        try:
            from gobby.runner_pipeline_runtime import build_pipeline_runtime

            runner.pipeline_execution_manager, runner.pipeline_executor = build_pipeline_runtime(
                runner,
                runner.project_id,
            )
            logger.info("Pipeline executor initialized at startup")
        except Exception:
            mark_service_degraded(runner, "pipeline_executor")
            logger.warning("Failed to initialize pipeline executor at startup", exc_info=True)
    elif runner.project_id and runner.workflow_loader is None:
        mark_service_degraded(runner, "pipeline_executor")
        logger.warning("Skipping pipeline executor initialization; WorkflowLoader is unavailable")

    runner.agent_runner = None
    try:
        runner.agent_runner = AgentRunner(
            db=runner.database,
            session_storage=runner.session_manager,
            max_agent_depth=5,
        )
        logger.debug("AgentRunner initialized")
    except Exception:
        mark_service_degraded(runner, "agent_runner")
        logger.exception("Failed to initialize AgentRunner")

    from gobby.storage.checkpoints import LocalCheckpointManager

    try:
        runner.agent_lifecycle_monitor = AgentLifecycleMonitor(
            agent_run_manager=LocalAgentRunManager(runner.database),
            db=runner.database,
            session_manager=runner.session_manager,
            clone_storage=runner.clone_storage,
            completion_registry=runner.completion_registry,
            task_manager=runner.task_manager,
            tmux_config=runner.config.tmux if hasattr(runner.config, "tmux") else None,
            checkpoint_storage=LocalCheckpointManager(runner.database),
            worktree_storage=runner.worktree_storage,
            stuck_detector=StuckDetector(
                runner.database,
                progress_tracker=ProgressTracker(runner.database),
            ),
            run_db=runner.db_executor.run,
        )
    except Exception:
        mark_service_degraded(runner, "agent_lifecycle_monitor")
        logger.warning("Failed to initialize AgentLifecycleMonitor", exc_info=True)
        runner.agent_lifecycle_monitor = None
    if runner.agent_runner is not None and runner.agent_lifecycle_monitor is not None:
        runner.agent_runner.agent_lifecycle_monitor = runner.agent_lifecycle_monitor

    runner.lifecycle_manager = SessionLifecycleManager(
        db=runner.database,
        config=runner.config.session_lifecycle,
        memory_manager=runner.memory_manager,
        llm_service=runner.llm_service,
        session_summary_config=runner.config.session_summary,
        kg_queue_config=runner.config.knowledge_graph_queue,
        memory_dream_config=getattr(getattr(runner.config, "memory", None), "dream", None),
    )

    runner.cron_storage = None
    runner.cron_scheduler = None
    runner.system_automation_loop = None
    try:
        try:
            from gobby.storage.cron import CronJobStorage

            runner.cron_storage = CronJobStorage(runner.database)
        except Exception:
            mark_service_degraded(runner, "cron_storage")
            logger.exception("Failed to initialize CronJobStorage")
            raise _CronDependencyUnavailable from None

        try:
            from gobby.scheduler.executor import CronExecutor

            cron_executor = CronExecutor(
                storage=runner.cron_storage,
                agent_runner=runner.agent_runner,
                pipeline_executor=runner.pipeline_executor,
                services=runner,
                config=runner.config.cron,
                run_db=runner.db_executor.run,
            )
        except Exception:
            mark_service_degraded(runner, "cron_executor")
            logger.exception("Failed to initialize CronExecutor")
            raise _CronDependencyUnavailable from None

        pipeline_heartbeat = _init_pipeline_heartbeat(runner)

        try:
            from gobby.system_automation import SystemAutomationLoop

            runner.system_automation_loop = SystemAutomationLoop(
                db=runner.database,
                config=runner.config,
                services=runner,
                config_store=runner.config_store,
                pipeline_heartbeat=pipeline_heartbeat,
                run_db=runner.db_executor.run,
            )
        except Exception:
            mark_service_degraded(runner, "system_automation_loop")
            logger.exception("Failed to initialize SystemAutomationLoop")

        for job_name in RETIRED_SYSTEM_CRON_JOBS:
            try:
                retired_job = runner.cron_storage.get_job_by_name(job_name)
                if retired_job and retired_job.enabled:
                    runner.cron_storage.update_job(
                        retired_job.id,
                        enabled=False,
                        next_run_at=None,
                    )
                    logger.info("Disabled retired system cron job: %s", job_name)
            except Exception as e:
                logger.warning(
                    "Failed to disable retired system cron job %s: %s",
                    job_name,
                    e,
                    exc_info=True,
                )

        from gobby.storage.projects import LocalProjectManager

        pm = LocalProjectManager(runner.database)

        memory_dream_config = getattr(getattr(runner.config, "memory", None), "dream", None)
        if memory_dream_config is None:
            logger.debug("Skipping memory dream cron registration; memory.dream config missing")
        elif runner.memory_manager is None:
            mark_service_degraded(runner, "memory_dream_cron")
            logger.warning("Skipping memory dream cron registration; MemoryManager is unavailable")
        else:
            try:
                from gobby.memory.dream.cron import register_memory_dream_cron

                registered = register_memory_dream_cron(
                    cron_storage=runner.cron_storage,
                    cron_executor=cron_executor,
                    memory_manager=runner.memory_manager,
                    dream_config=memory_dream_config,
                    llm_service=runner.llm_service,
                    project_id=runner.project_id,
                    daemon_config=runner.config,
                )
                logger.debug("Memory dream cron handlers registered: %s", registered)
            except Exception:
                mark_service_degraded(runner, "memory_dream_cron")
                logger.exception("Failed to register memory dream cron handler")

        runner.code_index_pruner = None
        runner.code_index_nightly_reindexer = None
        if runner.code_indexer is not None:
            try:
                from gobby.code_index.prune import (
                    CodeIndexPruner,
                    register_code_index_prune_cron,
                )

                runner.code_index_pruner = CodeIndexPruner(runner.code_indexer)
                register_code_index_prune_cron(
                    cron_storage=runner.cron_storage,
                    cron_executor=cron_executor,
                    pruner=runner.code_index_pruner,
                    project_id=runner.project_id,
                )
                logger.debug("Code index prune cron handler registered")
            except Exception as e:
                runner.code_index_pruner = None
                mark_service_degraded(runner, "code_index_pruner")
                logger.exception("Failed to register code index prune cron handler: %s", e)

            try:
                from gobby.code_index.nightly_reindex import (
                    CodeIndexNightlyFullReindexer,
                    register_code_index_nightly_reindex_cron,
                )

                runner.code_index_nightly_reindexer = CodeIndexNightlyFullReindexer(
                    runner.code_indexer
                )
                register_code_index_nightly_reindex_cron(
                    cron_storage=runner.cron_storage,
                    cron_executor=cron_executor,
                    reindexer=runner.code_index_nightly_reindexer,
                    config=runner.config.code_index,
                    project_id=runner.project_id,
                )
                logger.debug("Code index nightly full reindex cron handler registered")
            except Exception as e:
                runner.code_index_nightly_reindexer = None
                mark_service_degraded(runner, "code_index_nightly_reindexer")
                logger.exception(
                    "Failed to register code index nightly full reindex cron handler: %s",
                    e,
                )

            try:
                from gobby.code_index.codewiki_nightly import (
                    register_codewiki_nightly_crons,
                )

                # Codewiki freshness must cover every project the memory dream
                # judges per-project, not just the runner: each project's sweep
                # reads its resolved vault's _meta/truth_digest.json, so a
                # project whose codewiki is never refreshed is judged against a
                # stale or absent digest. Register a nightly refresh for the
                # runner project plus every memory-bearing project with a repo
                # path.
                codewiki_targets: dict[str, tuple[str, str]] = {}
                current_project = pm.get(runner.project_id) if runner.project_id else None
                if current_project is not None and current_project.repo_path:
                    codewiki_targets[current_project.id] = (
                        current_project.name,
                        current_project.repo_path,
                    )

                if runner.memory_manager is not None:
                    # A far-future cutoff makes every live memory "due", so the
                    # dream enumeration returns every project that has memories
                    # — the exact set the per-project sweep will judge.
                    all_memories_cutoff = "9999-12-31T23:59:59+00:00"
                    try:
                        memory_scopes = runner.memory_manager.list_dream_scopes(
                            redream_cutoff=all_memories_cutoff
                        )
                    except Exception as enum_err:
                        logger.warning(
                            "Failed to enumerate memory-bearing projects for codewiki: %s",
                            enum_err,
                            exc_info=True,
                        )
                        memory_scopes = []
                    for memory_scope in memory_scopes:
                        memory_project_id = memory_scope.project_id
                        if memory_project_id is None or memory_project_id in codewiki_targets:
                            continue
                        memory_project = pm.get(memory_project_id)
                        if memory_project is not None and memory_project.repo_path:
                            codewiki_targets[memory_project_id] = (
                                memory_project.name,
                                memory_project.repo_path,
                            )

                if not codewiki_targets:
                    logger.debug(
                        "Skipping nightly codewiki cron registration; no project has a repo path"
                    )
                else:
                    registered_count = register_codewiki_nightly_crons(
                        cron_storage=runner.cron_storage,
                        cron_executor=cron_executor,
                        projects=[
                            (project_id, project_name, repo_path)
                            for project_id, (project_name, repo_path) in codewiki_targets.items()
                        ],
                        wiki_config=runner.config.wiki,
                    )
                    logger.debug(
                        "Codewiki nightly cron handlers registered for %d project(s)",
                        registered_count,
                    )
            except Exception as e:
                mark_service_degraded(runner, "codewiki_nightly_cron")
                logger.exception("Failed to register codewiki nightly cron handler: %s", e)
        elif getattr(runner.config.code_index, "enabled", False):
            mark_service_degraded(runner, "code_index_maintenance")
            logger.warning(
                "Skipping code index maintenance registration; code indexer is unavailable"
            )

        try:
            from gobby.scheduler.scheduler import CronScheduler

            runner.cron_scheduler = CronScheduler(
                storage=runner.cron_storage,
                executor=cron_executor,
                config=runner.config.cron,
                run_db=runner.db_executor.run,
            )
            logger.debug("CronScheduler initialized")
        except Exception:
            runner.system_automation_loop = None
            mark_service_degraded(runner, "cron_scheduler")
            logger.exception("Failed to initialize CronScheduler")
    except _CronDependencyUnavailable:
        pass
    except Exception:
        runner.system_automation_loop = None
        mark_service_degraded(runner, "cron_scheduler")
        logger.exception("Failed to register cron handlers")

    if runner.memory_manager is not None:
        try:
            from gobby.memory.dream.cron import reconcile_interrupted_dream_runs

            interrupted_runs = reconcile_interrupted_dream_runs(runner.memory_manager)
            if interrupted_runs:
                logger.info(
                    "Reconciled %d orphaned memory dream run(s) to interrupted after restart: %s",
                    len(interrupted_runs),
                    ", ".join(interrupted_runs),
                )
        except Exception:
            mark_service_degraded(runner, "memory_dream_reconciliation")
            logger.exception("Failed to reconcile orphaned memory dream runs")

    runner.communications_manager = None
    if hasattr(runner.config, "communications") and runner.config.communications.enabled:
        try:
            from gobby.communications.manager import CommunicationsManager
            from gobby.storage.communications import LocalCommunicationsStore

            comms_store = LocalCommunicationsStore(runner.database)
            runner.communications_manager = CommunicationsManager(
                config=runner.config.communications,
                store=comms_store,
                secret_store=runner.secret_store,
                session_store=runner.session_manager,
            )
            logger.debug("CommunicationsManager initialized")
        except Exception:
            mark_service_degraded(runner, "communications_manager")
            logger.exception("Failed to initialize CommunicationsManager")
