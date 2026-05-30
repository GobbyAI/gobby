"""HookManager subsystem factory.

Creates and wires all HookManager subsystems in a single factory method.
Extracted from HookManager.__init__() as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from gobby.autonomous.progress_tracker import ProgressTracker
from gobby.autonomous.stop_registry import StopRegistry
from gobby.autonomous.stuck_detector import StuckDetector
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.health_monitor import HealthMonitor
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.hooks.session_types import HookSessionManager
from gobby.hooks.skill_manager import HookSkillManager
from gobby.hooks.webhooks import WebhookDispatcher
from gobby.memory.manager import MemoryManager
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.hook_assembler import HookTranscriptAssembler
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
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

    session: SessionManager
    session_task: SessionTaskManager
    memory: LocalMemoryManager
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
    database: HubDatabase
    daemon_client: DaemonClient
    transcript_processor: ClaudeTranscriptParser
    session_task_manager: SessionTaskManager
    memory_storage: LocalMemoryManager
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
        agent_runner: Any | None,
        completion_registry: Any | None,
        get_machine_id: Callable[[], str],
        resolve_project_id: Callable[[str | None, str | None], str],
        database: HubDatabase | None = None,
        session_manager: SessionManager | None = None,
        code_index_trigger: Any | None = None,
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
            agent_runner: Optional AgentRunner for agent-scoped workflow completion
            completion_registry: Optional CompletionEventRegistry for wait-step wakeups
            get_machine_id: Callable returning machine ID
            resolve_project_id: Callable resolving project ID from (project_id, cwd)
            database: Optional database instance to share with daemon services
            session_manager: Optional SessionManager instance to share with daemon services

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
        if session_manager is not None:
            if database is not None and database is not session_manager.db:
                raise ValueError("database and session_manager.db must reference the same object")
            resolved_database = session_manager.db
        else:
            resolved_database = database or cls._create_database(config)
        daemon_client = DaemonClient(
            host=daemon_host,
            port=daemon_port,
            timeout=5.0,
            logger=hook_logger,
        )
        transcript_processor = ClaudeTranscriptParser(logger_instance=hook_logger)

        # Create storage layer
        storage = cls._create_storage(
            resolved_database,
            logger=hook_logger,
            config=config,
            session_manager=session_manager,
        )

        # Initialize autonomous components
        autonomous = cls._create_autonomous(resolved_database)

        # Initialize memory system
        mem_manager = cls._create_memory(resolved_database, config)

        # Initialize workflow engine
        workflow_components = cls._create_workflow_engine(
            resolved_database,
            config,
            llm_service,
            transcript_processor,
            mem_manager,
            storage,
            autonomous,
            memory_sync_manager,
            task_sync_manager,
            agent_runner,
            completion_registry,
            tool_proxy_getter,
            resolve_project_id,
            broadcaster,
        )

        # Initialize webhooks
        webhook_dispatcher = cls._create_webhooks(config)

        # Use the same canonical SessionManager instance for both storage-
        # and service-level hooks access so caches and DB operations stay aligned.
        session_mgr = storage.session

        session_coordinator = SessionCoordinator(
            session_storage=cast(HookSessionManager, storage.session),
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

        # Build synchronous call_tool wrapper for EventHandlers skill fallback
        call_tool_fn = cls._build_sync_call_tool(tool_proxy_getter, loop, hook_logger)

        event_handlers = EventHandlers(
            session_manager=cast(HookSessionManager, session_mgr),
            workflow_handler=workflow_components.handler,
            session_task_manager=storage.session_task,
            message_processor=message_processor,
            task_manager=storage.task,
            worktree_manager=storage.worktree,
            session_coordinator=session_coordinator,
            skill_manager=workflow_components.skill_manager,
            skills_config=config.skills if config else None,
            memory_recall_config=config.memory_recall if config else None,
            call_tool=call_tool_fn,
            workflow_config=config.workflow if config else None,
            get_machine_id=get_machine_id,
            resolve_project_id=resolve_project_id,
            code_index_trigger=code_index_trigger,
            logger=hook_logger,
        )

        return HookManagerComponents(
            config=config,
            database=resolved_database,
            daemon_client=daemon_client,
            transcript_processor=transcript_processor,
            session_task_manager=storage.session_task,
            memory_storage=storage.memory,
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
    def _build_sync_call_tool(
        tool_proxy_getter: Any | None,
        loop: asyncio.AbstractEventLoop | None,
        logger: logging.Logger,
    ) -> Any:
        """Build a synchronous call_tool callable for EventHandlers.

        Wraps the async tool_proxy_getter in a blocking closure so
        event handlers can make MCP calls (e.g., gobby-skills fallback)
        from the synchronous hook dispatch context.

        Returns None if tool_proxy_getter or loop is unavailable.
        """
        if not tool_proxy_getter or not loop:
            return None

        def _sync_call_tool(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
            proxy = tool_proxy_getter()
            if not proxy:
                return None

            async def _do() -> dict[str, Any]:
                result: dict[str, Any] = await proxy.call_tool(server, tool, args)
                return result

            if loop.is_running():
                try:
                    future = asyncio.run_coroutine_threadsafe(_do(), loop)
                    return future.result(timeout=10)
                except Exception as e:
                    logger.debug(f"_sync_call_tool: threadsafe failed: {e}")
                    return None
            else:
                try:
                    return asyncio.run(_do())
                except Exception as e:
                    logger.debug(f"_sync_call_tool: asyncio.run failed: {e}")
                    return None

        return _sync_call_tool

    @staticmethod
    def _build_inline_mcp_dispatcher(
        tool_proxy_getter: Any | None,
    ) -> Callable[..., Any] | None:
        """Build an async dispatcher for inline mcp_call effects.

        Used by the rule engine to dispatch inject_result mcp_calls within
        the effect loop, ensuring atomicity with sibling set_variable effects.
        Runs as a coroutine since evaluate() is already async.

        Returns None if tool_proxy_getter is unavailable.
        """
        if not tool_proxy_getter:
            return None

        _logger = logging.getLogger("gobby.workflows.engine.inline_dispatch")

        async def dispatcher(
            server: str,
            tool: str,
            arguments: dict[str, Any],
            event: Any,
        ) -> dict[str, Any] | None:
            proxy = tool_proxy_getter()
            if not proxy:
                _logger.warning("inline_mcp_dispatcher: tool_proxy_getter returned None")
                return {"success": False, "error": "tool_proxy_getter returned None"}

            try:
                # Inject event context (mirrors dispatch_mcp_calls behavior)
                args = dict(arguments)
                if event:
                    if "session_id" not in args:
                        args["session_id"] = event.metadata.get("_platform_session_id", "")
                    if "prompt_text" not in args:
                        args["prompt_text"] = event.data.get("prompt") if event.data else None
                    if "project_path" not in args:
                        args["project_path"] = event.metadata.get("project_path") or None
                    # Map prompt_text to query for tools that expect it
                    if "query" not in args and args.get("prompt_text"):
                        args["query"] = args["prompt_text"]

                result = await proxy.call_tool(
                    server,
                    tool,
                    args,
                    strip_unknown=True,
                    enforce_workflow=False,
                )
                success = isinstance(result, dict) and result.get("success", True)
                return {
                    "success": success,
                    "inject_result": True,
                    "result": result,
                }
            except Exception as exc:
                _logger.warning(
                    f"inline_mcp_dispatcher: {server}/{tool} failed: {exc}",
                    exc_info=True,
                )
                return {"success": False, "error": str(exc)}

        return dispatcher

    @staticmethod
    def _create_database(config: Any | None) -> HubDatabase:
        database_url = getattr(config, "database_url", None) if config is not None else None
        if database_url:
            from gobby.storage.hub.postgres import PostgresHubDatabase

            return PostgresHubDatabase(database_url)

        from gobby.storage.hub.runtime import open_runtime_hub_database

        return open_runtime_hub_database(apply_migrations=False)

    @staticmethod
    def _create_storage(
        database: HubDatabase,
        *,
        logger: logging.Logger,
        config: Any | None,
        session_manager: SessionManager | None = None,
    ) -> _Storage:
        session = session_manager or SessionManager(database, logger_instance=logger, config=config)
        session_task = SessionTaskManager(database)
        return _Storage(
            session=session,
            session_task=session_task,
            memory=LocalMemoryManager(database),
            task=LocalTaskManager(database),
            agent_run=LocalAgentRunManager(database),
            worktree=LocalWorktreeManager(database),
        )

    @staticmethod
    def _create_autonomous(database: HubDatabase) -> _Autonomous:
        progress_tracker = ProgressTracker(database)
        return _Autonomous(
            stop_registry=StopRegistry(database),
            progress_tracker=progress_tracker,
            stuck_detector=StuckDetector(database, progress_tracker=progress_tracker),
        )

    @staticmethod
    def _create_memory(database: HubDatabase, config: Any | None) -> MemoryManager:
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
        database: HubDatabase,
        config: Any | None,
        llm_service: LLMService | None,
        transcript_processor: Any,
        memory_manager: MemoryManager,
        storage: _Storage,
        autonomous: _Autonomous,
        memory_sync_manager: Any | None,
        task_sync_manager: Any | None,
        agent_runner: Any | None,
        completion_registry: Any | None,
        tool_proxy_getter: Any | None,
        resolve_project_id: Callable[[str | None, str | None], str],
        broadcaster: Any | None,
    ) -> _WorkflowComponents:
        from gobby.mcp_proxy.metrics_events import MetricsEventStore
        from gobby.workflows.engine.core import RuleEngine
        from gobby.workflows.templates import TemplateEngine

        loader = WorkflowLoader(db=database)
        template_engine = TemplateEngine()
        metrics_event_store = MetricsEventStore(database)
        project_id = resolve_project_id(None, None)
        skill_manager = HookSkillManager(
            db=database,
            metrics_event_store=metrics_event_store,
            project_id=project_id,
        )
        # Build inline mcp_call dispatcher for inject_result atomicity.
        # Dispatches mcp_calls within the rule engine's effect loop so
        # set_variable effects that follow only fire on success.
        inline_dispatcher = HookManagerFactory._build_inline_mcp_dispatcher(tool_proxy_getter)

        rule_engine = RuleEngine(
            db=database,
            skill_manager=skill_manager,
            metrics_event_store=metrics_event_store,
            mcp_dispatcher=inline_dispatcher,
            runner=agent_runner,
            completion_registry=completion_registry,
            task_manager=storage.task,
        )

        pipeline_executor = None
        try:
            from gobby.storage.pipelines import LocalPipelineExecutionManager
            from gobby.workflows.pipeline_executor import PipelineExecutor

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
            session_manager=storage.session,
            session_task_manager=storage.session_task,
            config=config,
        )
        return _WorkflowComponents(
            loader=loader,
            template_engine=template_engine,
            skill_manager=skill_manager,
            pipeline_executor=pipeline_executor,
            handler=handler,
        )
