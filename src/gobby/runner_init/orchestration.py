"""Workflow, agent, cron, and communication setup for GobbyRunner."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.runner import AgentRunner
from gobby.autonomous.progress_tracker import ProgressTracker
from gobby.autonomous.stuck_detector import StuckDetector
from gobby.runner_init.services import mark_service_degraded
from gobby.sessions.lifecycle import SessionLifecycleManager

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.projects.purge import GraphCleaner, VectorCleaner
    from gobby.runner import GobbyRunner
    from gobby.system_automation import PipelineHeartbeatService

logger = logging.getLogger(__name__)

RETIRED_SYSTEM_CRON_JOBS = ("gobby:conductor-tick", "gobby:pipeline-heartbeat")


class _CronDependencyUnavailable(Exception):
    """Stop cron setup after a required dependency already logged its failure."""


class _WakeWriteServices:
    manager: Any = None
    coordinator: Any = None


_WAKE_WRITE_SERVICES = _WakeWriteServices()


def _wake_write_services() -> tuple[Any, Any]:
    if _WAKE_WRITE_SERVICES.manager is None or _WAKE_WRITE_SERVICES.coordinator is None:
        raise RuntimeError("wake write services are not bound")
    return _WAKE_WRITE_SERVICES.manager, _WAKE_WRITE_SERVICES.coordinator


def bind_wake_write_services(manager: Any, coordinator: Any) -> None:
    """Bind composition-root write services for daemon wake delivery."""
    _WAKE_WRITE_SERVICES.manager = manager
    _WAKE_WRITE_SERVICES.coordinator = coordinator


async def _send_tmux_session_wake(
    identity: str,
    message: str,
    *,
    submit: bool = False,
    escape_before_submit: bool = False,
) -> None:
    from gobby.agents.tmux.text_injection import TMUX_TEXT_ENTER_DELAY_SECONDS
    from gobby.terminals.runtime import Delivered, IndeterminateWrite
    from gobby.terminals.write_coordinator import SequenceDelay, WriteRequest

    manager, coordinator = _wake_write_services()
    terminal = manager.get(identity)
    if terminal is None and hasattr(manager, "get_live_for_session"):
        terminal = manager.get_live_for_session(identity)
    if terminal is None and hasattr(manager, "get_live_by_session_name"):
        terminal = manager.get_live_by_session_name(identity)
    if terminal is None:
        raise RuntimeError(f"no terminal for wake identity {identity}")
    steps: list[WriteRequest | SequenceDelay] = []
    if not submit:
        steps.append(
            WriteRequest(
                terminal_id=terminal.id,
                action_key=f"wake:{terminal.id}",
                origin="automatic",
                kind="text",
                payload=message,
            )
        )
    else:
        literal_text = message.rstrip("\n")
        if escape_before_submit:
            steps.append(
                WriteRequest(
                    terminal_id=terminal.id,
                    action_key=f"wake:{terminal.id}",
                    origin="automatic",
                    kind="key",
                    payload="escape",
                )
            )
            if literal_text:
                steps.append(SequenceDelay(seconds=TMUX_TEXT_ENTER_DELAY_SECONDS))
        if literal_text:
            steps.append(
                WriteRequest(
                    terminal_id=terminal.id,
                    action_key=f"wake:{terminal.id}",
                    origin="automatic",
                    kind="text",
                    payload=literal_text,
                )
            )
            steps.append(SequenceDelay(seconds=TMUX_TEXT_ENTER_DELAY_SECONDS))
        steps.append(
            WriteRequest(
                terminal_id=terminal.id,
                action_key=f"wake:{terminal.id}",
                origin="automatic",
                kind="key",
                payload="enter",
            )
        )
    outcome = await coordinator.run_sequence(
        terminal.id,
        action_key=f"wake:{terminal.id}",
        origin="automatic",
        steps=steps,
    )
    if isinstance(outcome, IndeterminateWrite):
        raise outcome
    if not isinstance(outcome, Delivered):
        raise RuntimeError(f"wake write to {identity} failed")


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


def _reconcile_codewiki_dormant_state(runner: GobbyRunner) -> None:
    """Disable persisted CodeWiki cron rows without blocking scheduler startup."""
    from gobby.wiki.codewiki_dormant import reconcile_codewiki_crons_disabled

    cron_storage = runner.cron_storage
    if cron_storage is None:
        mark_service_degraded(runner, "codewiki_dormant_reconciliation")
        logger.warning("Skipping dormant CodeWiki reconciliation; cron storage is unavailable")
        return

    try:
        result = reconcile_codewiki_crons_disabled(cron_storage)
    except Exception:
        mark_service_degraded(runner, "codewiki_dormant_reconciliation")
        logger.exception("Failed to reconcile dormant CodeWiki cron rows")
        return

    if result.failed or result.residual_enabled:
        mark_service_degraded(runner, "codewiki_dormant_reconciliation")
        logger.warning(
            "CodeWiki cron reconciliation left enabled rows; failed=%s residual_enabled=%s",
            result.failed,
            result.residual_enabled,
        )


def _resolve_project_vector_cleaner(runner: GobbyRunner) -> VectorCleaner:
    from gobby.projects.purge import NoopProjectVectorCleaner, ProjectPurgeVectorStoreUnavailable
    from gobby.projects.vector_cleanup import ProjectVectorCleaner

    bundle = runner.config_runtime.capture()
    memory = bundle.services.get("memory_services")
    vector_store = getattr(memory, "vector_store", None)
    if vector_store is not None:
        return ProjectVectorCleaner(vector_store)
    if bundle.snapshot.active.databases.qdrant.url is not None:
        raise ProjectPurgeVectorStoreUnavailable(
            "Qdrant is configured but the runtime memory bundle is unavailable"
        )
    return NoopProjectVectorCleaner()


def _resolve_project_graph_cleaner(runner: GobbyRunner) -> GraphCleaner:
    from gobby.config.persistence import is_falkordb_enabled
    from gobby.projects.purge import NoopProjectGraphCleaner

    bundle = runner.config_runtime.capture()
    memory = bundle.services.get("memory_services")
    manager = getattr(memory, "memory_manager", None)
    graph_cleaner = getattr(manager, "kg_service", None)
    if graph_cleaner is not None:
        return cast("GraphCleaner", graph_cleaner)
    if is_falkordb_enabled(bundle.snapshot.active.databases):
        raise RuntimeError("FalkorDB is configured but graph cleanup is unavailable")
    return NoopProjectGraphCleaner()


def init_orchestration(runner: GobbyRunner, config: DaemonConfig) -> None:
    """Initialize workflows, pipelines, agents, cron, and communications."""
    runner.project_purge_service = None
    runner.embedding_switch_coordinator = None
    try:
        from gobby.ai.embedding_switch_service import EmbeddingSwitchCoordinator
        from gobby.storage.config_store import ConfigStore

        runner.embedding_switch_coordinator = EmbeddingSwitchCoordinator(
            config_store=ConfigStore(runner.database, secret_store=runner.secret_store),
            db=runner.database,
            fence=runner.project_write_fence,
            config_runtime=runner.config_runtime,
        )
    except Exception:
        mark_service_degraded(runner, "embedding_switch_coordinator")
        logger.exception("Failed to initialize embedding switch coordinator")
    runner.workflow_loader = None
    runner.pipeline_execution_manager = None
    runner.pipeline_executor = None
    try:
        from gobby.workflows.pipeline_loader import PipelineLoader

        runner.workflow_loader = PipelineLoader(db=runner.database)
    except Exception:
        mark_service_degraded(runner, "workflow_loader")
        logger.warning("Failed to initialize workflow loader", exc_info=True)

    from gobby.agents.attention_metadata import AttentionMetadataStore
    from gobby.agents.tmux import configure_tmux
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.events.wake import WakeDispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.attention import AttentionStateManager
    from gobby.storage.inter_session_messages import InterSessionMessageManager

    ism_manager = InterSessionMessageManager(runner.database)
    agent_run_manager = LocalAgentRunManager(
        runner.database,
        credential_manager=runner.managed_credential_manager,
    )
    configure_tmux(config.tmux)

    def publish_attention_event(payload: dict[str, object]) -> None:
        loop = runner.main_loop
        if loop is None or not loop.is_running() or loop.is_closed():
            return

        def publish() -> None:
            from gobby.runner_broadcasting import fire_agent_event

            entry_id = str(payload["entry_id"])
            run_id = payload.get("run_id")
            fire_agent_event(
                "attention_changed",
                str(run_id) if run_id is not None else entry_id,
                dict(payload),
            )

        loop.call_soon_threadsafe(publish)

    def publish_attention_notification(payload: dict[str, object]) -> None:
        loop = runner.main_loop
        communications = runner.communications_manager
        if loop is None or not loop.is_running() or loop.is_closed() or communications is None:
            return
        session_id = payload.get("session_id")
        future = asyncio.run_coroutine_threadsafe(
            communications.send_event(
                "attention.blocked",
                json.dumps(payload, sort_keys=True),
                project_id=runner.project_id,
                session_id=str(session_id) if session_id is not None else None,
            ),
            loop,
        )

        def log_failure(completed: Any) -> None:
            try:
                completed.result()
            except Exception:
                logger.warning("Failed to publish attention notification", exc_info=True)

        future.add_done_callback(log_failure)

    def publish_attention_metadata(payload: dict[str, object]) -> None:
        loop = runner.main_loop
        if loop is None or not loop.is_running() or loop.is_closed():
            return

        def publish() -> None:
            from gobby.runner_broadcasting import fire_agent_event

            entry_id = str(payload["entry_id"])
            fire_agent_event("attention_metadata_changed", entry_id, dict(payload))

        loop.call_soon_threadsafe(publish)

    runner.attention_manager = AttentionStateManager(
        runner.database,
        event_publisher=publish_attention_event,
        notification_publisher=publish_attention_notification,
    )
    runner.attention_metadata_store = AttentionMetadataStore(
        runner.attention_manager.ordering,
        event_publisher=publish_attention_metadata,
    )

    runner.wake_dispatcher = WakeDispatcher(
        session_manager=runner.session_manager,
        ism_manager=ism_manager,
        tmux_sender=_send_tmux_session_wake,
        tmux_pane_sender=_send_tmux_pane_wake,
        agent_run_manager=agent_run_manager,
        run_db=runner.db_executor.run,
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
        logger.warning("Skipping pipeline executor initialization; PipelineLoader is unavailable")

    runner.agent_runner = None
    try:
        runner.agent_runner = AgentRunner(
            db=runner.database,
            session_storage=runner.session_manager,
            max_agent_depth=5,
            credential_manager=runner.managed_credential_manager,
        )
        logger.debug("AgentRunner initialized")
    except Exception:
        mark_service_degraded(runner, "agent_runner")
        logger.exception("Failed to initialize AgentRunner")

    from gobby.storage.checkpoints import LocalCheckpointManager

    runner.detection_registry = DetectionManifestRegistry(runner.database)
    from gobby.storage.terminals import TerminalManager
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.native_runtime import HostManagerControl, NativeTerminalRuntime
    from gobby.terminals.tmux_runtime import configured_tmux_runtime

    runner.terminal_manager = TerminalManager(runner.database)
    runner.terminal_config = config.terminals
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.terminals.host_manager import TerminalHostManager

    host_config = getattr(config, "terminal_host", None) or TerminalHostConfig()
    runner.terminal_host_config = host_config
    runner.terminal_host_manager = TerminalHostManager(
        config=host_config,
        terminal_config=config.terminals,
        terminal_manager=runner.terminal_manager,
        run_manager=LocalAgentRunManager(runner.database),
        tmux_attach_history_lines=config.tmux.attach_history_lines,
    )
    terminal_runtime_registry = TerminalRuntimeRegistry()

    terminal_runtime_registry.register(configured_tmux_runtime())
    native_runtime = NativeTerminalRuntime(
        HostManagerControl(runner.terminal_host_manager),
        frame_host_epoch=str(getattr(runner.terminal_host_manager, "host_epoch", "") or ""),
        terminal_manager=runner.terminal_manager,
        spawn_in_doubt_seconds=config.terminals.spawn_in_doubt_seconds,
    )
    terminal_runtime_registry.register(native_runtime)
    runner.frame_client = getattr(runner.terminal_host_manager, "_frame_client", None)
    runner.terminal_runtime_registry = terminal_runtime_registry
    from gobby.terminals.services import TerminalServices
    from gobby.terminals.sync_bridge import TerminalEffectBridge
    from gobby.terminals.write_coordinator import WriteCoordinator

    runner.write_coordinator = WriteCoordinator(runner.terminal_manager, terminal_runtime_registry)
    bind_wake_write_services(runner.terminal_manager, runner.write_coordinator)
    # The wake dispatcher is built before the terminal manager exists, so its
    # row lookup is wired here; without it every interactive wake falls back to
    # the tmux pane and native/gterm sessions are never nudged.
    runner.wake_dispatcher.set_terminal_manager(runner.terminal_manager)
    # One TerminalServices for every in-process consumer that closes or writes
    # to terminals: the lifecycle monitor, build controls, dispatch cleanup, and
    # websocket session control all resolve this instance.
    runner.terminal_services = TerminalServices(
        manager=runner.terminal_manager,
        registry=runner.terminal_runtime_registry,
        coordinator=runner.write_coordinator,
    )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    runner.terminal_effect_bridge = (
        None
        if loop is None
        else TerminalEffectBridge(
            loop,
            runner.write_coordinator,
            timeout_seconds=config.terminals.hook_write_timeout_seconds,
            shutdown_timeout_seconds=config.terminals.hook_write_shutdown_timeout_seconds,
        )
    )
    if runner.agent_runner is not None:
        runner.agent_runner.terminal_manager = runner.terminal_manager
        runner.agent_runner.terminal_runtime_registry = runner.terminal_runtime_registry
        runner.agent_runner.terminal_config = runner.terminal_config
        runner.agent_runner.write_coordinator = runner.write_coordinator
        runner.agent_runner.terminal_services = runner.terminal_services
    try:
        runner.agent_lifecycle_monitor = AgentLifecycleMonitor(
            agent_run_manager=LocalAgentRunManager(
                runner.database,
                credential_manager=runner.managed_credential_manager,
            ),
            db=runner.database,
            detection_registry=runner.detection_registry,
            session_manager=runner.session_manager,
            clone_storage=runner.clone_storage,
            completion_registry=runner.completion_registry,
            task_manager=runner.task_manager,
            tmux_config=config.tmux if hasattr(config, "tmux") else None,
            checkpoint_storage=LocalCheckpointManager(runner.database),
            worktree_storage=runner.worktree_storage,
            stuck_detector=StuckDetector(
                runner.database,
                progress_tracker=ProgressTracker(runner.database),
            ),
            run_db=runner.db_executor.run,
            attention_manager=runner.attention_manager,
            attention_metadata_store=runner.attention_metadata_store,
            terminal_services=runner.terminal_services,
        )
    except Exception:
        mark_service_degraded(runner, "agent_lifecycle_monitor")
        logger.warning("Failed to initialize AgentLifecycleMonitor", exc_info=True)
        runner.agent_lifecycle_monitor = None
    if runner.agent_runner is not None and runner.agent_lifecycle_monitor is not None:
        runner.agent_runner.agent_lifecycle_monitor = runner.agent_lifecycle_monitor

    runner.lifecycle_manager = SessionLifecycleManager(
        db=runner.database,
        capture_bundle=runner.config_runtime.capture,
    )

    # Single daemon-owned admission/launch owner for memory dream runs; cron,
    # HTTP routes, and MCP tools all resolve this instance.
    runner.memory_dream_coordinator = None
    memory_dream_config = getattr(getattr(config, "memory", None), "dream", None)
    if memory_dream_config is not None and runner.memory_manager is not None:
        try:
            from gobby.memory.dream.coordinator import MemoryDreamCoordinator
            from gobby.memory.dream.service import MemoryDreamService

            runner.memory_dream_coordinator = MemoryDreamCoordinator(
                MemoryDreamService(
                    memory_manager=runner.memory_manager,
                    dream_config=memory_dream_config,
                    llm_service=runner.llm_service,
                    daemon_config=config,
                    current_project_id=runner.project_id,
                    capture_bundle=runner.config_runtime.capture,
                )
            )
        except Exception:
            mark_service_degraded(runner, "memory_dream_coordinator")
            logger.exception("Failed to initialize memory dream coordinator")

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
            # Bundled schedules are wall-clock local; rows installed before that
            # converge here rather than waiting for unrelated definition drift.
            runner.cron_storage.normalize_system_job_timezones()
        except Exception:
            logger.exception("Failed to normalize system cron schedule timezones")

        _reconcile_codewiki_dormant_state(runner)

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

        try:
            from gobby.code_index.gcode_gateway import GcodeGateway
            from gobby.gwiki_gateway import GwikiGateway
            from gobby.projects.gwiki_lock import GwikiProjectDrainBarrier
            from gobby.projects.purge import ProjectPurgeService, register_project_purge_cron
            from gobby.storage.projects import GLOBAL_PROJECT_ID

            runner.project_purge_service = ProjectPurgeService(
                db=runner.database,
                projects=pm,
                cron=runner.cron_storage,
                fence=runner.project_write_fence,
                gwiki_barrier=GwikiProjectDrainBarrier(runner.database),
                wiki_gateway=GwikiGateway(),
                code_gateway=GcodeGateway(),
                vector_cleaner=lambda: _resolve_project_vector_cleaner(runner),
                graph_cleaner=lambda: _resolve_project_graph_cleaner(runner),
            )
            register_project_purge_cron(
                runner.cron_storage,
                cron_executor,
                runner.project_purge_service,
                project_id=GLOBAL_PROJECT_ID,
            )
            logger.debug("Project purge cron handler registered")
        except Exception:
            runner.project_purge_service = None
            mark_service_degraded(runner, "project_purge_service")
            logger.exception("Failed to initialize project purge service")

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

        if runner.llm_service is None:
            mark_service_degraded(runner, "feedback_review_cron")
            logger.warning("Skipping feedback review cron registration; LLM service unavailable")
        else:
            try:
                from gobby.feedback.cron import register_feedback_review_cron
                from gobby.feedback.service import FeedbackReviewService

                feedback_review_config = config.session_feedback.review
                feedback_review_service = FeedbackReviewService(
                    runner.database,
                    runner.llm_service,
                    feedback_review_config,
                    runner.task_manager,
                )
                registered = register_feedback_review_cron(
                    cron_storage=runner.cron_storage,
                    cron_executor=cron_executor,
                    service=feedback_review_service,
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
                    register_code_index_nightly_repair_cron,
                )

                runner.code_index_nightly_repairer = CodeIndexNightlyRepairer(runner.code_indexer)
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
    if hasattr(config, "communications") and config.communications.enabled:
        try:
            from gobby.communications.manager import CommunicationsManager
            from gobby.storage.communications import LocalCommunicationsStore

            comms_store = LocalCommunicationsStore(runner.database)
            runner.communications_manager = CommunicationsManager(
                config=config.communications,
                store=comms_store,
                secret_store=runner.secret_store,
                session_store=runner.session_manager,
            )
            logger.debug("CommunicationsManager initialized")
        except Exception:
            mark_service_degraded(runner, "communications_manager")
            logger.exception("Failed to initialize CommunicationsManager")
