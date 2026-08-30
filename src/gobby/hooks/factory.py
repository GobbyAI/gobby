"""HookManager subsystem factory.

Creates and wires all HookManager subsystems in a single factory method.
Extracted from HookManager.__init__() as part of the Strangler Fig decomposition.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from gobby.autonomous.progress_tracker import ProgressTracker
from gobby.autonomous.stop_registry import StopRegistry
from gobby.autonomous.stuck_detector import StuckDetector
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import load_bootstrap
from gobby.config.tasks import DEFAULT_WORKFLOW_TIMEOUT_SECONDS
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.health_monitor import HealthMonitor
from gobby.hooks.mcp_result import mcp_call_succeeded
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.hooks.session_end_auto_link import SessionEndAutoLinkWorker
from gobby.hooks.session_types import HookSessionManager
from gobby.hooks.skill_manager import HookSkillManager
from gobby.hooks.webhooks import WebhookDispatcher
from gobby.memory.manager import MemoryManager
from gobby.sessions.transcripts import get_parser
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.daemon_client import DaemonClient
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.pipeline_loader import PipelineLoader

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from gobby.config.values import ConfigRuntimeReader
    from gobby.llm.service import LLMService
    from gobby.sessions.transcripts.base import TranscriptParser
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

    loader: PipelineLoader
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
    transcript_processor: Callable[..., TranscriptParser]
    session_task_manager: SessionTaskManager
    memory_storage: LocalMemoryManager
    task_manager: LocalTaskManager
    agent_run_manager: LocalAgentRunManager
    worktree_manager: LocalWorktreeManager
    stop_registry: StopRegistry
    progress_tracker: ProgressTracker
    stuck_detector: StuckDetector
    memory_manager: MemoryManager
    workflow_loader: PipelineLoader
    template_engine: Any  # TemplateEngine
    skill_manager: HookSkillManager
    pipeline_executor: Any  # PipelineExecutor | None
    workflow_handler: WorkflowHookHandler
    webhook_dispatcher: WebhookDispatcher
    session_manager: SessionManager
    session_coordinator: SessionCoordinator
    session_end_auto_link_worker: SessionEndAutoLinkWorker
    health_monitor: HealthMonitor
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
        llm_service_resolver: Callable[[], LLMService | None],
        config: Any | None,
        hook_logger: logging.Logger,
        loop: asyncio.AbstractEventLoop | None,
        broadcaster: Any | None,
        tool_proxy_getter: Any | None,
        message_processor_resolver: Callable[[], Any | None],
        agent_runner: Any | None,
        completion_registry: Any | None,
        config_runtime: ConfigRuntimeReader | None,
        get_machine_id: Callable[[], str | None],
        resolve_project_id: Callable[[str | None, str | None], str],
        database: HubDatabase | None = None,
        session_manager: SessionManager | None = None,
        code_index_trigger: Any | None = None,
        memory_manager: MemoryManager | None = None,
        terminal_manager: Any | None = None,
    ) -> HookManagerComponents:
        """Create all HookManager subsystems.

        Args:
            daemon_host: Daemon host for communication
            daemon_port: Daemon port for communication
            llm_service: Optional LLMService for multi-provider support
            llm_service_resolver: Resolves the current runtime LLM service
            config: Optional DaemonConfig instance
            hook_logger: Configured logger instance
            loop: Event loop for async operations
            broadcaster: Optional HookEventBroadcaster instance
            tool_proxy_getter: Callable returning ToolProxyService
            message_processor_resolver: Resolves the current SessionMessageProcessor
            agent_runner: Optional AgentRunner for agent-scoped workflow completion
            completion_registry: Optional CompletionEventRegistry for wait-step wakeups
            config_runtime: Runtime snapshot reader for live configuration policy
            get_machine_id: Callable returning machine ID
            resolve_project_id: Callable resolving project ID from (project_id, cwd)
            database: Optional database instance to share with daemon services
            session_manager: Optional SessionManager instance to share with daemon services
            memory_manager: Optional fully-wired MemoryManager (llm_service, vector
                store, embeddings, graph) shared from the daemon. When omitted, a
                keyword-only fallback manager is built from the database and config.

        Returns:
            HookManagerComponents with all wired subsystem instances
        """
        # Capture one typed configuration snapshot for the construction operation.
        # An explicitly injected config wins at construction; per-operation
        # resolver lambdas below still prefer the live runtime snapshot.
        if config is None:
            try:
                config = cls._resolve_config(config, config_runtime)
            except Exception as e:
                hook_logger.exception(
                    "Failed to load config in HookManager, using bootstrap config: %s",
                    e,
                )
                config = cls._resolve_config(None, None)

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
        transcript_processor = get_parser

        # Create storage layer
        storage = cls._create_storage(
            resolved_database,
            logger=hook_logger,
            config=config,
            session_manager=session_manager,
        )

        # Initialize autonomous components
        autonomous = cls._create_autonomous(resolved_database)

        # Initialize memory system — prefer the daemon's fully-wired manager so
        # hook-path recall uses the same semantic+graph search as the MCP path.
        mem_manager = memory_manager or cls._create_memory(
            resolved_database,
            config,
            llm_service,
            llm_service_resolver,
        )

        # Initialize workflow engine
        workflow_components = cls._create_workflow_engine(
            resolved_database,
            config,
            llm_service,
            llm_service_resolver,
            mem_manager,
            storage,
            autonomous,
            agent_runner,
            completion_registry,
            config_runtime,
            tool_proxy_getter,
            broadcaster,
            loop,
        )

        # Initialize webhooks
        webhook_dispatcher = cls._create_webhooks(config)

        # Use the same canonical SessionManager instance for both storage-
        # and service-level hooks access so caches and DB operations stay aligned.
        session_mgr = storage.session

        session_coordinator = SessionCoordinator(
            session_storage=cast(HookSessionManager, storage.session),
            message_processor_resolver=message_processor_resolver,
            agent_run_manager=storage.agent_run,
            worktree_manager=storage.worktree,
            logger=hook_logger,
        )
        session_end_auto_link_worker = SessionEndAutoLinkWorker(
            database=resolved_database,
            task_manager=storage.task,
            logger=hook_logger,
        )

        health_monitor = HealthMonitor(
            daemon_client=daemon_client,
            health_check_interval=config.daemon_health_check_interval if config else 10.0,
            logger=hook_logger,
        )

        # Build synchronous call_tool wrapper for EventHandlers skill fallback
        call_tool_fn = cls._build_sync_call_tool(tool_proxy_getter, loop, hook_logger)

        event_handlers = EventHandlers(
            session_manager=cast(HookSessionManager, session_mgr),
            workflow_handler=workflow_components.handler,
            session_task_manager=storage.session_task,
            message_processor_resolver=message_processor_resolver,
            task_manager=storage.task,
            progress_tracker=autonomous.progress_tracker,
            worktree_manager=storage.worktree,
            session_coordinator=session_coordinator,
            session_end_auto_link_worker=session_end_auto_link_worker,
            skill_manager=workflow_components.skill_manager,
            call_tool=call_tool_fn,
            workflow_config=config.workflow if config else None,
            workflow_config_resolver=lambda: cls._resolve_config(config, config_runtime).workflow,
            get_machine_id=get_machine_id,
            resolve_project_id=resolve_project_id,
            code_index_trigger=code_index_trigger,
            terminal_manager=terminal_manager,
            event_loop=loop,
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
            session_end_auto_link_worker=session_end_auto_link_worker,
            health_monitor=health_monitor,
            event_handlers=event_handlers,
        )

    @staticmethod
    def _resolve_config(config: Any | None, config_runtime: ConfigRuntimeReader | None) -> Any:
        """Resolve hook configuration from one runtime snapshot or bootstrap inputs."""
        if config_runtime is not None:
            try:
                return config_runtime.snapshot.active
            except Exception:
                if config is not None:
                    return config
        if config is not None:
            return config
        bootstrap = load_bootstrap(resolve_database_url=True)
        return DaemonConfig(**bootstrap.to_config_dict())

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
                    logger.debug("_sync_call_tool: threadsafe failed: %s", e)
                    return None
            else:
                try:
                    return asyncio.run(_do())
                except Exception as e:
                    logger.debug("_sync_call_tool: asyncio.run failed: %s", e)
                    return None

        return _sync_call_tool

    @staticmethod
    def _build_inline_mcp_dispatcher(
        tool_proxy_getter: Any | None,
        daemon_loop: asyncio.AbstractEventLoop | None,
    ) -> Callable[..., Any] | None:
        """Build an async dispatcher for inline mcp_call effects.

        Used by the rule engine to dispatch inject_result mcp_calls within
        the effect loop, ensuring atomicity with sibling set_variable effects.
        Loop-bound proxy work runs on the daemon loop even when evaluation
        runs on the isolated workflow loop.

        Returns None if tool_proxy_getter is unavailable.
        """
        if not tool_proxy_getter:
            return None

        _logger = logging.getLogger("gobby.workflows.engine.inline_dispatch")

        async def dispatch_on_daemon_loop(
            server: str,
            tool: str,
            arguments: dict[str, Any],
            event: Any,
        ) -> dict[str, Any] | None:
            proxy = tool_proxy_getter()
            if not proxy:
                _logger.warning("inline_mcp_dispatcher: tool_proxy_getter returned None")
                return {"success": False, "error": "tool_proxy_getter returned None"}

            from gobby.utils.session_context import (
                SeededContextTokens,
                reset_seeded_contexts,
                resolve_and_seed_contexts,
            )

            session_id_is_explicit = "session_id" in arguments
            tokens: SeededContextTokens | None = None
            try:
                # Inject event context (mirrors dispatch_mcp_calls behavior)
                args = dict(arguments)
                if event:
                    if "session_id" not in args:
                        args["session_id"] = event.metadata.get("_platform_session_id", "")
                    if args.get("prompt_text") is None:
                        args.pop("prompt_text", None)
                        event_prompt = event.data.get("prompt") if event.data else None
                        if isinstance(event_prompt, str):
                            args["prompt_text"] = event_prompt
                    if "project_path" not in args:
                        args["project_path"] = event.metadata.get("project_path") or None
                    # Map prompt_text to query for tools that expect it
                    if "query" not in args and args.get("prompt_text"):
                        args["query"] = args["prompt_text"]

                session_ref = args.get("session_id") or None
                session_ref_origin: Literal["explicit", "ambient"] = (
                    "explicit" if session_id_is_explicit else "ambient"
                )
                session_manager = proxy.session_manager
                tokens = await resolve_and_seed_contexts(
                    session_ref=session_ref,
                    session_manager=session_manager,
                    project_ref=event.project_id if event else None,
                    session_ref_origin=session_ref_origin,
                    project_ref_is_fallback=True,
                    db=(session_manager.db if session_manager else None),
                )

                if not args.get("project_path"):
                    from gobby.utils.project_context import _current_project_context

                    project_context = _current_project_context.get()
                    if project_context and project_context.get("project_path"):
                        args["project_path"] = project_context["project_path"]

                result = await proxy.call_tool(
                    server,
                    tool,
                    args,
                    session_id=tokens.resolved_session_id,
                    strip_unknown=True,
                    enforce_workflow=False,
                )
                success = mcp_call_succeeded(result)
                return {
                    "success": success,
                    "inject_result": True,
                    "result": result,
                }
            except Exception as exc:
                _logger.warning(
                    "inline_mcp_dispatcher: %s/%s failed: %s",
                    server,
                    tool,
                    exc,
                    exc_info=True,
                )
                return {"success": False, "error": str(exc)}
            finally:
                if tokens is not None:
                    reset_seeded_contexts(tokens)

        async def dispatcher(
            server: str,
            tool: str,
            arguments: dict[str, Any],
            event: Any,
        ) -> dict[str, Any] | None:
            if daemon_loop is None or daemon_loop.is_closed() or not daemon_loop.is_running():
                error = "daemon event loop is unavailable"
                _logger.warning("inline_mcp_dispatcher: %s", error)
                return {"success": False, "error": error}

            running_loop = asyncio.get_running_loop()
            if running_loop is daemon_loop:
                return await dispatch_on_daemon_loop(server, tool, arguments, event)

            dispatch_coro = dispatch_on_daemon_loop(server, tool, arguments, event)
            try:
                future = asyncio.run_coroutine_threadsafe(dispatch_coro, daemon_loop)
            except RuntimeError:
                dispatch_coro.close()
                error = "daemon event loop is unavailable"
                _logger.warning("inline_mcp_dispatcher: %s", error)
                return {"success": False, "error": error}

            try:
                return await asyncio.wrap_future(future)
            except asyncio.CancelledError:
                future.cancel()
                raise

        return dispatcher

    @staticmethod
    def _create_database(config: Any | None) -> HubDatabase:
        database_url = getattr(config, "database_url", None) if config is not None else None
        if database_url and config is not None:
            from gobby.storage.hub.postgres import PostgresHubDatabase

            return PostgresHubDatabase(database_url, pool_config=config.postgres_pool)

        from gobby.storage.hub.postgres import PostgresHubDatabase

        bootstrap = load_bootstrap(resolve_database_url=True)
        if not bootstrap.database_url:
            raise RuntimeError("PostgreSQL database URL is not configured")
        return PostgresHubDatabase(
            bootstrap.database_url,
            pool_config=bootstrap.postgres_pool,
        )

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
    def _create_memory(
        database: HubDatabase,
        config: Any | None,
        llm_service: LLMService | None = None,
        llm_service_resolver: Callable[[], LLMService | None] | None = None,
    ) -> MemoryManager:
        memory_config = config.memory if config and hasattr(config, "memory") else None
        if not memory_config:
            from gobby.config.persistence import MemoryConfig

            memory_config = MemoryConfig()
        return MemoryManager(
            database,
            memory_config,
            llm_service=llm_service,
            llm_service_resolver=llm_service_resolver,
        )

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
        llm_service_resolver: Callable[[], LLMService | None],
        memory_manager: MemoryManager,
        storage: _Storage,
        autonomous: _Autonomous,
        agent_runner: Any | None,
        completion_registry: Any | None,
        config_runtime: ConfigRuntimeReader | None,
        tool_proxy_getter: Any | None,
        broadcaster: Any | None,
        daemon_loop: asyncio.AbstractEventLoop | None,
    ) -> _WorkflowComponents:
        from gobby.mcp_proxy.metrics_events import MetricsEventStore
        from gobby.skills.materialization import get_skill_script_materializer
        from gobby.workflows.engine.core import RuleEngine
        from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
        from gobby.workflows.templates import TemplateEngine

        loader = PipelineLoader(db=database)
        template_engine = TemplateEngine()
        metrics_event_store = MetricsEventStore(database)
        skill_manager = HookSkillManager(
            db=database,
            metrics_event_store=metrics_event_store,
        )
        # Build inline mcp_call dispatcher for inject_result atomicity.
        # Dispatches mcp_calls within the rule engine's effect loop so
        # set_variable effects that follow only fire on success.
        inline_dispatcher = HookManagerFactory._build_inline_mcp_dispatcher(
            tool_proxy_getter,
            daemon_loop,
        )

        rule_engine = RuleEngine(
            db=database,
            skill_manager=skill_manager,
            metrics_event_store=metrics_event_store,
            mcp_dispatcher=inline_dispatcher,
            runner=agent_runner,
            completion_registry=completion_registry,
            task_manager=storage.task,
            config_runtime=config_runtime,
            skill_script_materializer=get_skill_script_materializer(database),
        )

        pipeline_executor = None
        try:
            from gobby.storage.pipelines import LocalPipelineExecutionManager
            from gobby.workflows.pipeline_executor import PipelineExecutor

            pipeline_mgr = LocalPipelineExecutionManager(database, None)
            pipeline_executor = PipelineExecutor(
                db=database,
                execution_manager=pipeline_mgr,
                llm_service=llm_service,
                llm_service_resolver=llm_service_resolver,
                loader=loader,
                template_engine=template_engine,
                tool_proxy_getter=tool_proxy_getter,
                session_manager=storage.session,
                pipeline_config=config.pipelines if config else None,
                pipeline_config_resolver=lambda: HookManagerFactory._resolve_config(
                    config, config_runtime
                ).pipelines,
            )
        except Exception as e:
            logger.debug("Pipeline executor not available: %s", e)

        workflow_timeout = DEFAULT_WORKFLOW_TIMEOUT_SECONDS
        workflow_enabled = True
        if config:
            workflow_timeout = config.workflow.timeout
            workflow_enabled = config.workflow.enabled

        evaluation_runtime = WorkflowEvaluationRuntime()
        try:
            handler = WorkflowHookHandler(
                timeout=workflow_timeout,
                enabled=workflow_enabled,
                rule_engine=rule_engine,
                task_manager=storage.task,
                session_manager=storage.session,
                session_task_manager=storage.session_task,
                config=config,
                config_resolver=lambda: HookManagerFactory._resolve_config(config, config_runtime),
                llm_service_resolver=llm_service_resolver,
                evaluation_runtime=evaluation_runtime,
            )
        except Exception:
            evaluation_runtime.shutdown()
            raise
        return _WorkflowComponents(
            loader=loader,
            template_engine=template_engine,
            skill_manager=skill_manager,
            pipeline_executor=pipeline_executor,
            handler=handler,
        )
