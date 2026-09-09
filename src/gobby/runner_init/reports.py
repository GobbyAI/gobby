"""Compose synthesis publication with the daemon scheduler and terminal source stores."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gobby.reports.service import SynthesisReporter
from gobby.storage.cron_models import CronJob

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.runner import GobbyRunner
    from gobby.scheduler.executor import CronExecutor


def init_synthesis_reports(
    runner: GobbyRunner, config: DaemonConfig, executor: CronExecutor
) -> None:
    if runner.project_id is None or runner.agent_runner is None or runner.git_manager is None:
        raise RuntimeError("Synthesis reporting requires an agent runner and repository project")
    cron_storage = runner.cron_storage
    if cron_storage is None:
        raise RuntimeError("Synthesis reporting requires cron storage")
    reporter = SynthesisReporter(
        db=runner.database,
        runner=runner.agent_runner,
        session_manager=runner.session_manager,
        completion_registry=runner.completion_registry,
        task_manager=runner.task_manager,
        worktree_storage=runner.worktree_storage,
        git_manager=runner.git_manager,
        daemon_config=config,
        project_id=runner.project_id,
    )
    reporter.store.recover()
    if runner.feedback_review_service is not None:
        runner.feedback_review_service.store.report_project_id = runner.project_id
    if runner.memory_dream_coordinator is not None:
        runner.memory_dream_coordinator.service.store.report_project_id = runner.project_id

    async def publish_reports(_job: CronJob) -> str:
        count = await reporter.run_pending()
        return f"synthesis reports: processed {count} publication(s)"

    name = "synthesis-reports"
    executor.register_handler(name, publish_reports)
    action = {"handler": name, "timeout_seconds": 9600, "restart_protected": False}
    existing = cron_storage.get_job_by_name(name)
    if existing is None:
        cron_storage.create_job(
            project_id=runner.project_id,
            name=name,
            schedule_type="interval",
            interval_seconds=60,
            action_type="handler",
            action_config=action,
            enabled=True,
            is_system=True,
        )
    else:
        cron_storage.reconcile_system_job_definition(
            existing.id,
            action_type="handler",
            action_config=action,
            description="Publish terminal feedback and Dream synthesis reports",
            schedule_type="interval",
            cron_expr=None,
            interval_seconds=60,
            run_at=None,
        )
