"""Daemon cron and system automation initialization."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gobby.runner_init.project_purge import init_project_purge
from gobby.runner_init.services import mark_service_degraded

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.runner import GobbyRunner
    from gobby.system_automation import PipelineHeartbeatService

logger = logging.getLogger(__name__)

RETIRED_SYSTEM_CRON_JOBS = ("gobby:conductor-tick", "gobby:pipeline-heartbeat", "synthesis-reports")


class _CronDependencyUnavailable(Exception):
    """Stop cron setup after a required dependency already logged its failure."""


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
            agent_run_manager=LocalAgentRunManager(
                runner.database,
                status_notifier=runner.session_manager._notify_status_transition,
                credential_manager=runner.managed_credential_manager,
            ),
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


def init_cron(runner: GobbyRunner, config: DaemonConfig) -> None:
    """Initialize cron dependencies, handlers and scheduler without changing ownership."""
    memory_dream_config = getattr(getattr(config, "memory", None), "dream", None)
    runner.cron_storage = None
    runner.cron_scheduler = None
    runner.system_automation_loop = None
    runner.feedback_review_service = None
    try:
        try:
            from gobby.storage.cron import CronJobStorage

            runner.cron_storage = CronJobStorage(runner.database)
        except Exception:
            mark_service_degraded(runner, "cron_storage")
            logger.exception("Failed to initialize CronJobStorage")
            raise _CronDependencyUnavailable from None

        try:
            # Bundled schedules are wall-clock local; rows installed before that
            # converge here rather than waiting for unrelated definition drift.
            runner.cron_storage.normalize_system_job_timezones()
        except Exception:
            logger.exception("Failed to normalize system cron schedule timezones")

        try:
            from gobby.scheduler.executor import CronExecutor

            cron_executor = CronExecutor(
                storage=runner.cron_storage,
                agent_runner=runner.agent_runner,
                pipeline_executor=runner.pipeline_executor,
                services=runner,
                config=config.cron,
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
                capture_bundle=runner.config_runtime.capture,
                services=runner,
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

        init_project_purge(runner, pm, runner.cron_storage, cron_executor)

        if memory_dream_config is None:
            logger.debug("Skipping memory dream cron registration; memory.dream config missing")
        elif runner.memory_dream_coordinator is None:
            mark_service_degraded(runner, "memory_dream_cron")
            logger.warning(
                "Skipping memory dream cron registration; dream coordinator is unavailable"
            )
        else:
            try:
                from gobby.memory.dream.cron import register_memory_dream_cron

                registered = register_memory_dream_cron(
                    cron_storage=runner.cron_storage,
                    cron_executor=cron_executor,
                    coordinator=runner.memory_dream_coordinator,
                    dream_config=memory_dream_config,
                    project_id=runner.project_id,
                )
                logger.debug("Memory dream cron handlers registered: %s", registered)
            except Exception:
                mark_service_degraded(runner, "memory_dream_cron")
                logger.exception("Failed to register memory dream cron handler")

        try:
            from gobby.agents.workspace_backups import register_agent_workspace_cron

            register_agent_workspace_cron(
                cron_storage=runner.cron_storage,
                cron_executor=cron_executor,
            )
        except Exception:
            mark_service_degraded(runner, "agent_workspace_cron")
            logger.exception("Failed to register agent workspace cron handler")

        try:
            from gobby.feedback.agent import FeedbackReviewerAgent
            from gobby.feedback.cron import register_feedback_review_cron
            from gobby.feedback.service import FeedbackReviewService

            feedback_review_config = config.session_feedback.review
            feedback_reviewer = FeedbackReviewerAgent(
                agent_name=feedback_review_config.agent,
                db=runner.database,
                runner=runner.agent_runner,
                session_manager=runner.session_manager,
                completion_registry=runner.completion_registry,
                project_id=runner.project_id,
                project_path=(
                    str(runner.git_manager.repo_path) if runner.git_manager is not None else None
                ),
                git_manager=runner.git_manager,
                daemon_config=config,
            )
            runner.feedback_review_service = FeedbackReviewService(
                runner.database,
                feedback_reviewer,
                feedback_review_config,
                runner.task_manager,
            )
            interrupted = runner.feedback_review_service.store.mark_running_interrupted()
            if interrupted:
                logger.info("Marked %d orphaned feedback review run(s) interrupted", interrupted)
            registered = register_feedback_review_cron(
                cron_storage=runner.cron_storage,
                cron_executor=cron_executor,
                service=runner.feedback_review_service,
                config=feedback_review_config,
                project_id=runner.project_id,
            )
            logger.debug("Feedback review cron handlers registered: %s", registered)
        except Exception:
            mark_service_degraded(runner, "feedback_review_cron")
            logger.exception("Failed to register feedback review cron handler")

        runner.code_index_pruner = None
        runner.code_index_nightly_repairer = None
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
                from gobby.code_index.nightly_repair import (
                    CodeIndexNightlyRepairer,
                    nightly_reconcile_apply_switch,
                    register_code_index_nightly_repair_cron,
                )

                runner.code_index_nightly_repairer = CodeIndexNightlyRepairer(
                    runner.code_indexer,
                    apply_enabled=nightly_reconcile_apply_switch(runner.config_runtime),
                )
                register_code_index_nightly_repair_cron(
                    cron_storage=runner.cron_storage,
                    cron_executor=cron_executor,
                    repairer=runner.code_index_nightly_repairer,
                    config=config.code_index,
                    project_id=runner.project_id,
                )
                logger.debug("Code index nightly repair cron handler registered")
            except Exception as e:
                runner.code_index_nightly_repairer = None
                mark_service_degraded(runner, "code_index_nightly_repairer")
                logger.exception(
                    "Failed to register code index nightly repair cron handler: %s",
                    e,
                )

        elif getattr(config.code_index, "enabled", False):
            mark_service_degraded(runner, "code_index_maintenance")
            logger.warning(
                "Skipping code index maintenance registration; code indexer is unavailable"
            )

        try:
            from gobby.scheduler.scheduler import CronScheduler

            runner.cron_scheduler = CronScheduler(
                storage=runner.cron_storage,
                executor=cron_executor,
                capture_bundle=runner.config_runtime.capture,
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
