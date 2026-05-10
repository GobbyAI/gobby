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

    async def _tmux_send(tmux_session_name: str, message: str) -> None:
        from gobby.agents.tmux import get_tmux_session_manager

        mgr = get_tmux_session_manager()
        await mgr.send_keys(tmux_session_name, message)

    async def _tmux_pane_send(
        pane_id: str,
        message: str,
        tmux_socket_path: str | None,
    ) -> None:
        text = message.rstrip("\n")
        tmux_cmd = ["tmux"]
        if tmux_socket_path:
            tmux_cmd.extend(["-S", tmux_socket_path])
        proc = await asyncio.create_subprocess_exec(
            *tmux_cmd,
            "send-keys",
            "-t",
            pane_id,
            "-l",
            text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"tmux send-keys to {pane_id} timed out after 10s") from None
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux send-keys to {pane_id} failed: {stderr.decode(errors='replace')}"
            )

        proc = await asyncio.create_subprocess_exec(
            *tmux_cmd,
            "send-keys",
            "-t",
            pane_id,
            "Enter",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"tmux send-keys to {pane_id} timed out after 10s") from None
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux send-keys to {pane_id} failed: {stderr.decode(errors='replace')}"
            )

    runner.wake_dispatcher = WakeDispatcher(
        session_manager=runner.session_manager,
        ism_manager=ism_manager,
        tmux_sender=_tmux_send,
        tmux_pane_sender=_tmux_pane_send,
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

    from gobby.storage.agents import LocalAgentRunManager
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
        )
    except Exception as e:
        logger.warning(f"Failed to initialize AgentLifecycleMonitor: {e}")
        runner.agent_lifecycle_monitor = None

    runner.lifecycle_manager = SessionLifecycleManager(
        db=runner.database,
        config=runner.config.session_lifecycle,
        memory_manager=runner.memory_manager,
        llm_service=runner.llm_service,
        memory_sync_manager=runner.memory_sync_manager,
    )

    runner.cron_storage = None
    runner.cron_scheduler = None
    try:
        from gobby.scheduler.executor import CronExecutor
        from gobby.scheduler.scheduler import CronScheduler
        from gobby.storage.cron import CronJobStorage

        runner.cron_storage = CronJobStorage(runner.database)
        cron_executor = CronExecutor(
            storage=runner.cron_storage,
            agent_runner=runner.agent_runner,
            pipeline_executor=runner.pipeline_executor,
            services=runner,
        )
        cron_executor.register_handler(
            "dispatch.tick",
            cron_executor._execute_dispatcher,
        )
        if runner.project_id:
            from gobby.runner import install_dispatcher_cron_row

            dispatcher_job = install_dispatcher_cron_row(
                runner.database,
                project_id=runner.project_id,
            )
            if dispatcher_job.enabled:
                runner.cron_storage.wake_system_job(dispatcher_job.id)
            logger.info("Installed system cron job: gobby:dispatcher")

        try:
            from gobby.storage.agents import LocalAgentRunManager
            from gobby.workflows.pipeline_heartbeat import PipelineHeartbeat

            if runner.pipeline_execution_manager is None:
                raise RuntimeError("pipeline_execution_manager required for heartbeat")

            heartbeat = PipelineHeartbeat(
                execution_manager=runner.pipeline_execution_manager,
                task_manager=runner.task_manager,
                agent_run_manager=LocalAgentRunManager(runner.database),
                session_manager=runner.session_manager,
                run_db=runner.db_executor.run,
            )
            cron_executor.register_handler("pipeline_heartbeat", heartbeat)

            existing = runner.cron_storage.get_job_by_name("gobby:pipeline-heartbeat")
            if not existing and runner.project_id:
                runner.cron_storage.create_job(
                    project_id=runner.project_id,
                    name="gobby:pipeline-heartbeat",
                    description="Safety net: detects stalled pipelines and marks dead executions as failed",
                    schedule_type="interval",
                    interval_seconds=60,
                    action_type="handler",
                    action_config={"handler": "pipeline_heartbeat"},
                    enabled=True,
                    is_system=True,
                )
                logger.info("Created system cron job: gobby:pipeline-heartbeat")
            elif existing:
                if not existing.is_system:
                    runner.cron_storage.db.execute(
                        "UPDATE cron_jobs SET is_system = 1 WHERE id = ?",
                        (existing.id,),
                    )
                runner.cron_storage.reconcile_system_job_definition(
                    existing.id,
                    description=(
                        "Safety net: detects stalled pipelines and marks dead executions as failed"
                    ),
                    schedule_type="interval",
                    cron_expr=None,
                    interval_seconds=60,
                    run_at=None,
                    timezone="UTC",
                    action_type="handler",
                    action_config={"handler": "pipeline_heartbeat"},
                )
                refreshed = runner.cron_storage.get_job(existing.id)
                if refreshed and refreshed.enabled:
                    runner.cron_storage.wake_system_job(refreshed.id)
            logger.debug("PipelineHeartbeat handler registered")
        except Exception as e:
            logger.error(f"Failed to register pipeline heartbeat: {e}")

        try:
            conductor_job = runner.cron_storage.get_job_by_name("gobby:conductor-tick")
            if conductor_job and conductor_job.enabled:
                runner.cron_storage.update_job(conductor_job.id, enabled=False, next_run_at=None)
                logger.info("Disabled retired system cron job: gobby:conductor-tick")
        except Exception as e:
            logger.warning(f"Failed to disable retired conductor cron job: {e}")

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
