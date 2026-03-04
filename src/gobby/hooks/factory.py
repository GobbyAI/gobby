"""HookManager subsystem factory.

Creates and wires all HookManager subsystems in a single factory method.
Extracted from HookManager.__init__() as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.autonomous.progress_tracker import ProgressTracker
from gobby.autonomous.stop_registry import StopRegistry
from gobby.autonomous.stuck_detector import StuckDetector
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.health_monitor import HealthMonitor
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.hooks.skill_manager import HookSkillManager
from gobby.hooks.webhooks import WebhookDispatcher
from gobby.memory.manager import MemoryManager
from gobby.sessions.manager import SessionManager
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.hook_assembler import HookTranscriptAssembler
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.session_messages import LocalSessionMessageManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.daemon_client import DaemonClient
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.loader import WorkflowLoader

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from gobby.llm.service import LLMService
    from gobby.workflows.pipeline_executor import PipelineExecutor
    from gobby.workflows.templates import TemplateEngine

logger = logging.getLogger(__name__)


@dataclass
class _Storage:
    """Container for storage managers."""

    session: LocalSessionManager
    session_task: SessionTaskManager
    memory: LocalMemoryManager
    message: LocalSessionMessageManager
    task: LocalTaskManager
    agent_run: LocalAgentRunManager
    worktree: LocalWorktreeManager


@dataclass
class _Autonomous:
    """Container for autonomous subsystem components."""

    stop_registry: StopRegistry
    progress_tracker: ProgressTracker
    stuck_detector: StuckDetector


@dataclass
class _WorkflowComponents:
    """Container for workflow engine components."""

    loader: WorkflowLoader
    template_engine: TemplateEngine
    skill_manager: HookSkillManager
    pipeline_executor: PipelineExecutor | None
    handler: WorkflowHookHandler


@dataclass
class HookManagerComponents:
    """All subsystem instances created by HookManagerFactory."""

    config: Any  # DaemonConfig | None
    database: LocalDatabase
    daemon_client: DaemonClient
    transcript_processor: ClaudeTranscriptParser
    session_storage: LocalSessionManager
    session_task_manager: SessionTaskManager
    memory_storage: LocalMemoryManager
    message_manager: LocalSessionMessageManager
    task_manager: LocalTaskManager
    agent_run_manager: LocalAgentRunManager
    worktree_manager: LocalWorktreeManager
    stop_registry: StopRegistry
    progress_tracker: ProgressTracker
    stuck_detector: StuckDetector
    memory_manager: MemoryManager
    workflow_loader: WorkflowLoader
    template_engine: Any  # TemplateEngine
    skill_manager: HookSkillManager
    pipeline_executor: Any  # PipelineExecutor | None
    workflow_handler: WorkflowHookHandler
    webhook_dispatcher: WebhookDispatcher
    session_manager: SessionManager
    session_coordinator: SessionCoordinator
    health_monitor: HealthMonitor
    hook_assembler: HookTranscriptAssembler
    event_handlers: EventHandlers


class HookManagerFactory:
    """Factory for creating and wiring all HookManager subsystems."""

    @classmethod
    def create(
        cls,
        *,
        daemon_host: str,
        daemon_port: int,
        llm_service: LLMService | None,
        config: Any | None,
        hook_logger: logging.Logger,
        loop: asyncio.AbstractEventLoop | None,
        broadcaster: Any | None,
        tool_proxy_getter: Any | None,
        message_processor: Any | None,
        memory_sync_manager: Any | None,
        task_sync_manager: Any | None,
        get_machine_id: Callable[[], str],
        resolve_project_id: Callable[[str | None, str | None], str],
    ) -> HookManagerComponents:
        """Create all HookManager subsystems.

        Args:
            daemon_host: Daemon host for communication
            daemon_port: Daemon port for communication
            llm_service: Optional LLMService for multi-provider support
            config: Optional DaemonConfig instance
            hook_logger: Configured logger instance
            loop: Event loop for async operations
            broadcaster: Optional HookEventBroadcaster instance
            tool_proxy_getter: Callable returning ToolProxyService
            message_processor: SessionMessageProcessor instance
            memory_sync_manager: Optional MemorySyncManager instance
            task_sync_manager: Optional TaskSyncManager instance
            get_machine_id: Callable returning machine ID
            resolve_project_id: Callable resolving project ID from (project_id, cwd)

        Returns:
            HookManagerComponents with all wired subsystem instances
        """
        # Load configuration if not provided
        if not config:
            try:
                from gobby.config.app import load_config

                config = load_config()
            except Exception as e:
                hook_logger.error(
                    f"Failed to load config in HookManager, using defaults: {e}",
                    exc_info=True,
                )

        # Initialize core components
        database = cls._create_database(config)
        daemon_client = DaemonClient(
            host=daemon_host,
            port=daemon_port,
            timeout=5.0,
            logger=hook_logger,
        )
        transcript_processor = ClaudeTranscriptParser(logger_instance=hook_logger)

        # Create storage layer
        storage = cls._create_storage(database)

        # Initialize autonomous components
        autonomous = cls._create_autonomous(database)

        # Initialize memory system
        mem_manager = cls._create_memory(database, config)

        # Initialize workflow engine
        workflow_components = cls._create_workflow_engine(
            database,
            config,
            llm_service,
            transcript_processor,
            mem_manager,
            storage,
            autonomous,
            memory_sync_manager,
            task_sync_manager,
            tool_proxy_getter,
            resolve_project_id,
            broadcaster,
        )

        # Initialize webhooks
        webhook_dispatcher = cls._create_webhooks(config)

        # Initialize session management
        session_mgr = SessionManager(
            session_storage=storage.session,
            logger_instance=hook_logger,
            config=config,
        )

        session_coordinator = SessionCoordinator(
            session_storage=storage.session,
            message_processor=message_processor,
            agent_run_manager=storage.agent_run,
            worktree_manager=storage.worktree,
            logger=hook_logger,
        )

        health_monitor = HealthMonitor(
            daemon_client=daemon_client,
            health_check_interval=config.daemon_health_check_interval if config else 10.0,
            logger=hook_logger,
        )

        hook_assembler = HookTranscriptAssembler()

        event_handlers = EventHandlers(
            session_manager=session_mgr,
            workflow_handler=workflow_components.handler,
            session_storage=storage.session,
            session_task_manager=storage.session_task,
            message_processor=message_processor,
            task_manager=storage.task,
            session_coordinator=session_coordinator,
            message_manager=storage.message,
            skill_manager=workflow_components.skill_manager,
            skills_config=config.skills if config else None,
            workflow_config=config.workflow if config else None,
            get_machine_id=get_machine_id,
            resolve_project_id=resolve_project_id,
            logger=hook_logger,
        )

        return HookManagerComponents(
            config=config,
            database=database,
            daemon_client=daemon_client,
            transcript_processor=transcript_processor,
            session_storage=storage.session,
            session_task_manager=storage.session_task,
            memory_storage=storage.memory,
            message_manager=storage.message,
            task_manager=storage.task,
            agent_run_manager=storage.agent_run,
            worktree_manager=storage.worktree,
            stop_registry=autonomous.stop_registry,
            progress_tracker=autonomous.progress_tracker,
            stuck_detector=autonomous.stuck_detector,
            memory_manager=mem_manager,
            workflow_loader=workflow_components.loader,
            template_engine=workflow_components.template_engine,
            skill_manager=workflow_components.skill_manager,
            pipeline_executor=workflow_components.pipeline_executor,
            workflow_handler=workflow_components.handler,
            webhook_dispatcher=webhook_dispatcher,
            session_manager=session_mgr,
            session_coordinator=session_coordinator,
            health_monitor=health_monitor,
            hook_assembler=hook_assembler,
            event_handlers=event_handlers,
        )

    @staticmethod
    def _create_database(config: Any | None) -> LocalDatabase:
        if config and config.database_path:
            db_path = Path(config.database_path).expanduser()
            return LocalDatabase(db_path)
        return LocalDatabase()

    @staticmethod
    def _create_storage(database: LocalDatabase) -> _Storage:
        session = LocalSessionManager(database)
        session_task = SessionTaskManager(database)
        return _Storage(
            session=session,
            session_task=session_task,
            memory=LocalMemoryManager(database),
            message=LocalSessionMessageManager(database),
            task=LocalTaskManager(database),
            agent_run=LocalAgentRunManager(database),
            worktree=LocalWorktreeManager(database),
        )

    @staticmethod
    def _create_autonomous(database: LocalDatabase) -> _Autonomous:
        progress_tracker = ProgressTracker(database)
        return _Autonomous(
            stop_registry=StopRegistry(database),
            progress_tracker=progress_tracker,
            stuck_detector=StuckDetector(database, progress_tracker=progress_tracker),
        )

    @staticmethod
    def _create_memory(database: LocalDatabase, config: Any | None) -> MemoryManager:
        memory_config = config.memory if config and hasattr(config, "memory") else None
        if not memory_config:
            from gobby.config.persistence import MemoryConfig

            memory_config = MemoryConfig()
        return MemoryManager(database, memory_config)

    @staticmethod
    def _create_webhooks(config: Any | None) -> WebhookDispatcher:
        webhooks_config = None
        if config and hasattr(config, "hook_extensions"):
            webhooks_config = config.hook_extensions.webhooks
        if not webhooks_config:
            from gobby.config.extensions import WebhooksConfig

            webhooks_config = WebhooksConfig()
        return WebhookDispatcher(webhooks_config)

    @staticmethod
    def _create_workflow_engine(
        database: LocalDatabase,
        config: Any | None,
        llm_service: LLMService | None,
        transcript_processor: Any,
        memory_manager: MemoryManager,
        storage: _Storage,
        autonomous: _Autonomous,
        memory_sync_manager: Any | None,
        task_sync_manager: Any | None,
        tool_proxy_getter: Any | None,
        resolve_project_id: Callable[[str | None, str | None], str],
        broadcaster: Any | None,
    ) -> _WorkflowComponents:
        from gobby.workflows.rule_engine import RuleEngine
        from gobby.workflows.templates import TemplateEngine

        loader = WorkflowLoader(db=database)
        rule_engine = RuleEngine(db=database)
        template_engine = TemplateEngine()
        skill_manager = HookSkillManager(db=database)

        pipeline_executor = None
        try:
            from gobby.storage.pipelines import LocalPipelineExecutionManager
            from gobby.workflows.pipeline_executor import PipelineExecutor

            project_id = resolve_project_id(None, None)
            pipeline_mgr = LocalPipelineExecutionManager(database, project_id)
            pipeline_executor = PipelineExecutor(
                db=database,
                execution_manager=pipeline_mgr,
                llm_service=llm_service,
                loader=loader,
                template_engine=template_engine,
                tool_proxy_getter=tool_proxy_getter,
                session_manager=storage.session,
            )
        except Exception as e:
            logger.debug(f"Pipeline executor not available: {e}")

        workflow_timeout = 0.0
        workflow_enabled = True
        if config:
            workflow_timeout = config.workflow.timeout
            workflow_enabled = config.workflow.enabled

        try:
            _loop = asyncio.get_running_loop()
        except RuntimeError:
            _loop = None

        handler = WorkflowHookHandler(
            loop=_loop,
            timeout=workflow_timeout,
            enabled=workflow_enabled,
            rule_engine=rule_engine,
            task_manager=storage.task,
            session_task_manager=storage.session_task,
        )
        return _WorkflowComponents(
            loader=loader,
            template_engine=template_engine,
            skill_manager=skill_manager,
            pipeline_executor=pipeline_executor,
            handler=handler,
        )
