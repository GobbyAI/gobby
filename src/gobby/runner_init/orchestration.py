"""Workflow, agent, cron, and communication setup for GobbyRunner."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.runner import AgentRunner
from gobby.sessions.lifecycle import SessionLifecycleManager

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)

RETIRED_SYSTEM_CRON_JOBS = ("gobby:conductor-tick",)


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


def init_orchestration(runner: GobbyRunner) -> None:
    """Initialize workflows, pipelines, agents, cron, and communications."""
    runner.workflow_loader = None
    runner.pipeline_execution_manager = None
    runner.pipeline_executor = None
    try:
        from gobby.workflows.loader import WorkflowLoader

        runner.workflow_loader = WorkflowLoader(db=runner.database)
    except Exception as e:
        logger.warning(f"Failed to initialize workflow loader: {e}")

    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.events.wake import WakeDispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.inter_session_messages import InterSessionMessageManager

    ism_manager = InterSessionMessageManager(runner.database)
    agent_run_manager = LocalAgentRunManager(runner.database)

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
            from gobby.storage.pipelines import LocalPipelineExecutionManager as _LPEM
            from gobby.workflows.pipeline_executor import PipelineExecutor as _PE
            from gobby.workflows.templates import TemplateEngine

            runner.pipeline_execution_manager = _LPEM(
                db=runner.database, project_id=runner.project_id
            )
            runner.pipeline_executor = _PE(
                db=runner.database,
                execution_manager=runner.pipeline_execution_manager,
                llm_service=runner.llm_service,
                loader=runner.workflow_loader,
                template_engine=TemplateEngine(),
                session_manager=runner.session_manager,
                completion_registry=runner.completion_registry,
                run_db=runner.db_executor.run,
            )
            logger.info("Pipeline executor initialized at startup")
        except Exception as e:
            logger.warning(f"Failed to initialize pipeline executor at startup: {e}")

    runner.agent_runner = None
    try:
        runner.agent_runner = AgentRunner(
            db=runner.database,
            session_storage=runner.session_manager,
            max_agent_depth=5,
        )
        logger.debug("AgentRunner initialized")
    except Exception as e:
        logger.error(f"Failed to initialize AgentRunner: {e}")

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
            run_db=runner.db_executor.run,
        )
    except Exception as e:
        logger.warning(f"Failed to initialize AgentLifecycleMonitor: {e}")
        runner.agent_lifecycle_monitor = None
    if runner.agent_runner is not None and runner.agent_lifecycle_monitor is not None:
        runner.agent_runner.agent_lifecycle_monitor = runner.agent_lifecycle_monitor

    runner.lifecycle_manager = SessionLifecycleManager(
        db=runner.database,
        config=runner.config.session_lifecycle,
        memory_manager=runner.memory_manager,
        llm_service=runner.llm_service,
        memory_sync_manager=runner.memory_sync_manager,
    )

    runner.cron_storage = None
    runner.cron_scheduler = None
    runner.system_automation_loop = None
    try:
        from gobby.scheduler.executor import CronExecutor
        from gobby.scheduler.scheduler import CronScheduler
        from gobby.storage.cron import CronJobStorage
        from gobby.system_automation import (
            SystemAutomationLoop,
            remove_legacy_automation_cron_rows,
        )

        runner.cron_storage = CronJobStorage(runner.database)
        cron_executor = CronExecutor(
            storage=runner.cron_storage,
            agent_runner=runner.agent_runner,
            pipeline_executor=runner.pipeline_executor,
            services=runner,
        )
        removed_automation_jobs = remove_legacy_automation_cron_rows(runner.database)
        if removed_automation_jobs:
            logger.info("Removed %d legacy automation cron row(s)", removed_automation_jobs)

        pipeline_heartbeat = None
        try:
            from gobby.workflows.pipeline_heartbeat import PipelineHeartbeat

            if runner.pipeline_execution_manager is None:
                raise RuntimeError("pipeline_execution_manager required for heartbeat")

            pipeline_heartbeat = PipelineHeartbeat(
                execution_manager=runner.pipeline_execution_manager,
                task_manager=runner.task_manager,
                agent_run_manager=LocalAgentRunManager(runner.database),
                session_manager=runner.session_manager,
                run_db=runner.db_executor.run,
            )
            logger.debug("PipelineHeartbeat maintenance registered")
        except Exception as e:
            logger.error(f"Failed to initialize pipeline heartbeat maintenance: {e}")

        runner.system_automation_loop = SystemAutomationLoop(
            db=runner.database,
            config=runner.config,
            services=runner,
            config_store=runner.config_store,
            pipeline_heartbeat=pipeline_heartbeat,
            run_db=runner.db_executor.run,
        )

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
                logger.warning("Failed to disable retired system cron job %s: %s", job_name, e)

        from gobby.storage.projects import LocalProjectManager

        pm = LocalProjectManager(runner.database)

        try:
            from gobby.sync.linear import create_linear_sync_handler

            for project in pm.list():
                if project.linear_team_id:
                    handler = create_linear_sync_handler(
                        mcp_manager=runner.mcp_proxy,
                        task_manager=runner.task_manager,
                        project_id=project.id,
                        team_id=project.linear_team_id,
                        linear_project_id=project.linear_project_id,
                    )
                    handler_name = f"linear_sync:{project.id}"
                    cron_executor.register_handler(handler_name, handler)

                    job_name = f"gobby:linear-sync:{project.id}"
                    existing = runner.cron_storage.get_job_by_name(job_name)
                    if not existing:
                        runner.cron_storage.create_job(
                            project_id=project.id,
                            name=job_name,
                            description=f"Bidirectional Linear sync for project {project.name}",
                            schedule_type="interval",
                            interval_seconds=300,
                            action_type="handler",
                            action_config={"handler": handler_name},
                            enabled=True,
                        )
                        logger.info(f"Created system cron job: {job_name}")
            logger.debug("Linear sync handlers registered")
        except Exception as e:
            logger.error(f"Failed to register Linear sync handlers: {e}")

        try:
            from gobby.github_triage.cron import register_github_triage_cron

            registered = register_github_triage_cron(
                cron_storage=runner.cron_storage,
                cron_executor=cron_executor,
                db=runner.database,
                mcp_manager=runner.mcp_proxy,
                task_manager=runner.task_manager,
                project_manager=pm,
                memory_manager=runner.memory_manager,
                secret_store=runner.secret_store,
            )
            logger.debug("GitHub issue triage cron handlers registered: %s", registered)
        except Exception as e:
            logger.error(f"Failed to register GitHub issue triage cron handlers: {e}")

        try:
            from gobby.wiki.scheduled_jobs import (
                configured_wiki_cron_scopes,
                register_wiki_cron_jobs,
            )

            if runner.project_id:
                registered = register_wiki_cron_jobs(
                    cron_storage=runner.cron_storage,
                    cron_executor=cron_executor,
                    project_id=runner.project_id,
                    scopes=configured_wiki_cron_scopes(runner.config, runner.project_id),
                )
                logger.debug("Wiki cron handlers registered: %s", registered)
        except Exception as e:
            logger.error(f"Failed to register wiki cron handlers: {e}")

        runner.cron_scheduler = CronScheduler(
            storage=runner.cron_storage,
            executor=cron_executor,
            config=runner.config.cron,
        )
        logger.debug("CronScheduler initialized")
    except Exception as e:
        logger.error(f"Failed to initialize CronScheduler: {e}")

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
        except Exception as e:
            logger.error(f"Failed to initialize CommunicationsManager: {e}")
