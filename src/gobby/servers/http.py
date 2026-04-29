"""
HTTP server for Gobby daemon.

Provides a FastAPI-based HTTP server for REST endpoints, MCP tool proxying,
and session management. Local-first version: no platform auth, no remote sync.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from gobby.hooks.broadcaster import HookEventBroadcaster
from gobby.llm import create_llm_service
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.semantic_search import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    SemanticToolSearch,
)
from gobby.mcp_proxy.server import GobbyDaemonTools, create_mcp_server
from gobby.telemetry.instruments import inc_counter

if TYPE_CHECKING:
    from gobby.app_context import ServiceContainer
    from gobby.config.app import DaemonConfig
    from gobby.hooks.hook_manager import HookManager
    from gobby.llm import LLMService
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.mcp_proxy.tools.internal import InternalRegistryManager
    from gobby.servers.websocket.server import WebSocketServer
    from gobby.utils.tool_metrics import ToolMetricsManager

logger = logging.getLogger(__name__)

_STREAMABLE_HTTP_TERMINATE_TIMEOUT_SECONDS = 2.0


class HTTPServer:
    """
    FastAPI HTTP server for Gobby daemon.

    Handles MCP tool proxying, session management, and admin endpoints.
    Local-first version: no platform authentication, uses local SQLite storage.
    """

    def __init__(
        self,
        services: "ServiceContainer",
        port: int = 8000,
        test_mode: bool = False,
        codex_client: Any | None = None,
    ) -> None:
        """
        Initialize HTTP server.

        Args:
            services: ServiceContainer holding all dependencies
            port: Server port
            test_mode: Run in test mode (disable features that conflict with testing)
            codex_client: CodexAppServerClient instance for Codex integration
        """
        self.services = services
        self.port = port
        self.test_mode = test_mode
        self.codex_client = codex_client

        # WebSocket server reference (set by GobbyRunner after construction)
        self.websocket_server: WebSocketServer | None = None

        # Lazily-populated caches
        self._savings_tracker: Any | None = None

        self.broadcaster = HookEventBroadcaster(services.websocket_server, services.config)

        self._start_time: float = time.time()

        # Create LLM service if not provided in container (fallback)
        if not services.llm_service and services.config:
            try:
                services.llm_service = create_llm_service(services.config)
                logger.debug(
                    f"LLM service initialized with providers: {services.llm_service.enabled_providers}"
                )
            except Exception as e:
                logger.error(f"Failed to initialize LLM service: {e}")

        # Create MCP server instance
        self._mcp_server: Any | None = None
        self._internal_manager: InternalRegistryManager | None = None
        self._tools_handler: GobbyDaemonTools | None = None
        self._hook_manager: HookManager | None = None

        if services.mcp_manager:
            self._init_mcp_subsystems(services, port)

        from gobby.servers.app_factory import create_app

        self.app = create_app(self)
        self._running = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._daemon: Any = None  # Set externally by daemon

    def _init_mcp_subsystems(self, services: "ServiceContainer", port: int) -> None:
        """Initialize MCP proxy, internal registries, and semantic search."""
        assert services.mcp_manager is not None, "caller must check services.mcp_manager"
        # Determine WebSocket port
        ws_port = 60888
        cfg = services.config
        if cfg and hasattr(cfg, "websocket") and cfg.websocket:
            ws_port = cfg.websocket.port

        # Create a lazy getter for tool_proxy that will be available after
        # GobbyDaemonTools is created. This allows in-process agents to route
        # tool calls through the MCP proxy.
        def tool_proxy_getter() -> Any:
            if self._tools_handler is not None:
                return self._tools_handler.tool_proxy
            return None

        # Create merge managers if db available
        merge_storage = None
        merge_resolver = None
        inter_session_message_manager = None
        if self.services.mcp_db_manager:
            from gobby.storage.inter_session_messages import InterSessionMessageManager
            from gobby.storage.merge_resolutions import MergeResolutionManager
            from gobby.worktrees.merge.resolver import MergeResolver

            merge_storage = MergeResolutionManager(self.services.mcp_db_manager.db)
            merge_resolver = MergeResolver()
            merge_resolver.llm_service = services.llm_service
            merge_resolver._config = services.config.merge_resolution if services.config else None
            inter_session_message_manager = InterSessionMessageManager(
                self.services.mcp_db_manager.db
            )
            logger.debug("Merge resolution and inter-session messaging subsystems initialized")

        # Create TranscriptReader for JSONL + gzip fallback reads
        transcript_reader = None
        if services.session_manager:
            from gobby.sessions.transcript_reader import TranscriptReader

            archive_dir = None
            if services.config and hasattr(services.config, "sessions"):
                archive_dir = getattr(services.config.sessions, "transcript_archive_dir", None)
            transcript_reader = TranscriptReader(
                session_manager=services.session_manager,
                archive_dir=archive_dir,
            )
            services.transcript_reader = transcript_reader

        # Setup internal registries (gobby-tasks, gobby-memory, gobby-workflows, etc.)
        self._internal_manager = setup_internal_registries(
            _config=services.config,
            _session_manager=None,  # Not needed for internal registries
            memory_manager=services.memory_manager,
            task_manager=services.task_manager,
            db=services.mcp_db_manager.db if services.mcp_db_manager else None,
            sync_manager=services.task_sync_manager,
            task_validator=services.task_validator,
            session_manager=services.session_manager,
            metrics_manager=services.metrics_manager,
            llm_service=services.llm_service,
            agent_runner=services.agent_runner,
            worktree_storage=services.worktree_storage,
            clone_storage=services.clone_storage,
            git_manager=services.git_manager,
            merge_storage=merge_storage,
            merge_resolver=merge_resolver,
            project_id=services.project_id,
            tool_proxy_getter=tool_proxy_getter,
            inter_session_message_manager=inter_session_message_manager,
            pipeline_executor=services.pipeline_executor,
            workflow_loader=services.workflow_loader,
            pipeline_execution_manager=services.pipeline_execution_manager,
            hook_manager_resolver=lambda: self._hook_manager,
            config_store=services.config_store,
            config_setter=lambda c: setattr(services, "config", c),
            memory_sync_manager=services.memory_sync_manager,
            completion_registry=services.completion_registry,
            agent_lifecycle_monitor=services.agent_lifecycle_monitor,
            cron_scheduler=services.cron_scheduler,
            transcript_reader=services.transcript_reader,
            communications_manager=services.communications_manager,
        )
        registry_count = len(self._internal_manager)
        logger.debug(f"Internal registries initialized: {registry_count} registries")

        # Initialize tool summarizer config
        if services.config:
            from gobby.tools.summarizer import init_summarizer_config

            init_summarizer_config(
                services.config.tool_summarizer,
                db=services.database,
                llm_service=services.llm_service,
            )
            logger.debug("Tool summarizer config initialized")

        # Create semantic search instance if db available
        semantic_search = None
        if services.mcp_db_manager:
            openai_api_key = None
            if services.database:
                try:
                    from gobby.storage.secrets import SecretStore

                    secret_store = SecretStore(services.database)
                    openai_api_key = secret_store.get("openai_api_key")
                except Exception:
                    pass  # SecretStore unavailable — fall through to env var
            _emb_cfg = services.config.embeddings if services.config else None
            semantic_search = SemanticToolSearch(
                db=services.mcp_db_manager.db,
                openai_api_key=(
                    _emb_cfg.api_key if _emb_cfg and _emb_cfg.api_key else openai_api_key
                ),
                embedding_model=_emb_cfg.model if _emb_cfg else DEFAULT_EMBEDDING_MODEL,
                embedding_dim=_emb_cfg.dim if _emb_cfg else DEFAULT_EMBEDDING_DIM,
                api_base=_emb_cfg.api_base if _emb_cfg else None,
                vector_store=getattr(services, "vector_store", None),
            )
            logger.debug(
                "Semantic tool search initialized (dim=%s)",
                _emb_cfg.dim if _emb_cfg else DEFAULT_EMBEDDING_DIM,
            )

        # Create fallback resolver for alternative tool suggestions on error
        fallback_resolver = None
        if semantic_search and services.metrics_manager:
            from gobby.mcp_proxy.services.fallback import ToolFallbackResolver

            fallback_resolver = ToolFallbackResolver(
                semantic_search=semantic_search,
                metrics_manager=services.metrics_manager,
            )
            logger.debug("Fallback resolver initialized")

        # Create tools handler
        self._tools_handler = GobbyDaemonTools(
            mcp_manager=services.mcp_manager,
            daemon_port=port,
            websocket_port=ws_port,
            start_time=self._start_time,
            internal_manager=self._internal_manager,
            config=services.config,
            llm_service=services.llm_service,
            session_manager=services.session_manager,
            memory_manager=services.memory_manager,
            config_manager=services.mcp_db_manager,
            semantic_search=semantic_search,
            fallback_resolver=fallback_resolver,
            hook_manager_resolver=lambda: self._hook_manager,
        )
        self._mcp_server = create_mcp_server(self._tools_handler)
        logger.debug("MCP server initialized and will be mounted at /mcp")

    # Property accessors for services (delegate to container)
    @property
    def config(self) -> "DaemonConfig | None":
        return self.services.config

    @property
    def session_manager(self) -> Any:
        return self.services.session_manager

    @session_manager.setter
    def session_manager(self, value: Any) -> None:
        self.services.session_manager = value

    @property
    def task_manager(self) -> Any:
        return self.services.task_manager

    @task_manager.setter
    def task_manager(self, value: Any) -> None:
        self.services.task_manager = value

    @property
    def mcp_manager(self) -> "MCPClientManager | None":
        return self.services.mcp_manager

    @mcp_manager.setter
    def mcp_manager(self, value: "MCPClientManager | None") -> None:
        self.services.mcp_manager = value

    @property
    def llm_service(self) -> "LLMService | None":
        return self.services.llm_service

    @llm_service.setter
    def llm_service(self, value: "LLMService | None") -> None:
        self.services.llm_service = value

    @property
    def memory_manager(self) -> Any:
        return self.services.memory_manager

    @memory_manager.setter
    def memory_manager(self, value: Any) -> None:
        self.services.memory_manager = value

    @property
    def message_processor(self) -> Any:
        return self.services.message_processor

    @message_processor.setter
    def message_processor(self, value: Any) -> None:
        self.services.message_processor = value

    @property
    def metrics_manager(self) -> "ToolMetricsManager | None":
        return self.services.metrics_manager

    @metrics_manager.setter
    def metrics_manager(self, value: "ToolMetricsManager | None") -> None:
        self.services.metrics_manager = value

    @property
    def transcript_reader(self) -> Any:
        return self.services.transcript_reader

    @transcript_reader.setter
    def transcript_reader(self, value: Any) -> None:
        self.services.transcript_reader = value

    @property
    def _mcp_db_manager(self) -> Any:
        return self.services.mcp_db_manager

    @_mcp_db_manager.setter
    def _mcp_db_manager(self, value: Any) -> None:
        self.services.mcp_db_manager = value

    @property
    def skill_manager(self) -> Any:
        return self.services.skill_manager

    @skill_manager.setter
    def skill_manager(self, value: Any) -> None:
        self.services.skill_manager = value

    @property
    def hub_manager(self) -> Any:
        return self.services.hub_manager

    @hub_manager.setter
    def hub_manager(self, value: Any) -> None:
        self.services.hub_manager = value

    @property
    def tool_proxy(self) -> Any:
        """Get the ToolProxyService instance for routing tool calls with error enrichment."""
        if self._tools_handler is not None:
            return self._tools_handler.tool_proxy
        return None

    async def _terminate_streamable_http_sessions(self) -> None:
        """Best-effort termination of active FastMCP Streamable HTTP sessions."""
        if self._mcp_server is None:
            return

        try:
            session_manager = self._mcp_server.session_manager
        except (AttributeError, RuntimeError):
            return

        server_instances = getattr(session_manager, "_server_instances", None)
        if not isinstance(server_instances, dict) or not server_instances:
            return

        transports = list(server_instances.values())
        logger.debug(
            "Terminating %d active Streamable HTTP session(s)",
            len(transports),
        )
        pending: list[tuple[Any, asyncio.Task[None]]] = []
        for transport in transports:
            terminate = getattr(transport, "terminate", None)
            if not callable(terminate):
                continue
            pending.append((transport, asyncio.create_task(terminate())))

        results = await asyncio.gather(
            *(
                asyncio.wait_for(
                    task,
                    timeout=_STREAMABLE_HTTP_TERMINATE_TIMEOUT_SECONDS,
                )
                for _transport, task in pending
            ),
            return_exceptions=True,
        )

        # asyncio.wait_for cancels the inner task on timeout, and gather has
        # already awaited completion with return_exceptions=True. We only need
        # to classify results for logging — no secondary cancellation pass or
        # lingering cleanup is required.
        for (transport, _task), result in zip(pending, results, strict=False):
            if isinstance(result, TimeoutError):
                logger.warning(
                    "Timed out terminating Streamable HTTP session %s after %.1fs",
                    getattr(transport, "mcp_session_id", "<unknown>"),
                    _STREAMABLE_HTTP_TERMINATE_TIMEOUT_SECONDS,
                )
            elif isinstance(result, Exception):
                logger.warning(
                    "Failed to terminate Streamable HTTP session %s: %s",
                    getattr(transport, "mcp_session_id", "<unknown>"),
                    result,
                )

    def resolve_project_id(self, project_id: str | None, cwd: str | None) -> str:
        """
        Resolve project_id from cwd if not provided.

        If project_id is given, returns it directly.
        Otherwise, looks up project from .gobby/project.json in the cwd.

        Args:
            project_id: Optional explicit project ID
            cwd: Current working directory path

        Returns:
            Project ID from .gobby/project.json

        Raises:
            ValueError: If no project.json found (project not initialized)
        """
        from pathlib import Path

        if project_id:
            return project_id

        # Get cwd or use current directory
        working_dir = Path(cwd) if cwd else Path.cwd()

        # Look up project from .gobby/project.json
        from gobby.utils.project_context import get_project_context

        project_context = get_project_context(working_dir)
        if project_context and project_context.get("id"):
            return str(project_context["id"])

        # No project.json found - require explicit initialization
        raise ValueError(
            f"No .gobby/project.json found in {working_dir} or parents. "
            "Run 'gobby init' to initialize a project."
        )

    async def _process_shutdown(self) -> None:
        """
        Background task to perform graceful daemon shutdown.
        """
        start_time = time.perf_counter()

        try:
            logger.debug("Processing graceful shutdown")

            # Cancel pending background tasks immediately instead of polling
            pending_tasks_count = len(self._background_tasks)
            if pending_tasks_count > 0:
                logger.debug(
                    "Cancelling pending background tasks",
                    extra={"pending_tasks": pending_tasks_count},
                )

                # Cancel all tasks
                for task in self._background_tasks:
                    task.cancel()

                # Wait for cancellation to complete with a short timeout
                if self._background_tasks:
                    done, pending = await asyncio.wait(
                        self._background_tasks,
                        timeout=5.0,
                        return_when=asyncio.ALL_COMPLETED,
                    )

                    completed_count = len(done)
                    remaining_count = len(pending)

                    if remaining_count > 0:
                        logger.warning(
                            "Some background tasks did not cancel in time",
                            extra={
                                "completed": completed_count,
                                "remaining": remaining_count,
                            },
                        )
                    else:
                        logger.debug(
                            "All background tasks cancelled",
                            extra={"completed": completed_count},
                        )

            try:
                await self._terminate_streamable_http_sessions()
            except Exception as exc:
                logger.warning(
                    "Error terminating Streamable HTTP sessions during shutdown: %s",
                    exc,
                    exc_info=True,
                )

            # Disconnect all MCP servers
            if self.services.mcp_manager:
                logger.debug("Disconnecting MCP servers...")
                try:
                    await self.services.mcp_manager.disconnect_all()
                    logger.debug("MCP servers disconnected")
                except Exception as e:
                    logger.warning(f"Error disconnecting MCP servers: {e}")

            duration_seconds = time.perf_counter() - start_time
            inc_counter("shutdown_succeeded_total")

            logger.debug(
                "Shutdown processed",
                extra={"duration_seconds": duration_seconds},
            )

        except Exception as e:
            duration_seconds = time.perf_counter() - start_time
            inc_counter("shutdown_failed_total")

            logger.error(
                f"Shutdown processing failed: {e}",
                exc_info=True,
                extra={"duration_seconds": duration_seconds},
            )


async def create_server(
    services: "ServiceContainer",
    port: int = 60887,
    test_mode: bool = False,
) -> HTTPServer:
    """
    Create HTTP server instance.

    Args:
        services: ServiceContainer holding dependencies
        port: Port to listen on
        test_mode: Enable test mode

    Returns:
        Configured HTTPServer instance
    """
    return HTTPServer(
        services=services,
        port=port,
        test_mode=test_mode,
    )


async def run_server(
    server: HTTPServer,
    host: str = "0.0.0.0",  # nosec B104 # local daemon needs network access
    workers: int = 1,
    limit_concurrency: int | None = 1000,
    limit_max_requests: int | None = None,
    timeout_keep_alive: int = 5,
    timeout_graceful_shutdown: int = 30,
) -> None:
    """
    Run HTTP server with production-ready Uvicorn configuration.

    Args:
        server: HTTPServer instance
        host: Host to bind to (default: 0.0.0.0 for all interfaces)
        workers: Number of worker processes (default: 1 for async)
        limit_concurrency: Max concurrent connections (default: 1000)
        limit_max_requests: Max requests before worker restart (None = unlimited)
        timeout_keep_alive: Keep-alive timeout in seconds (default: 5)
        timeout_graceful_shutdown: Graceful shutdown timeout in seconds (default: 30)
    """
    import uvicorn

    config = uvicorn.Config(
        server.app,
        host=host,
        port=server.port,
        log_level="info",
        access_log=True,
        log_config=None,
        limit_concurrency=limit_concurrency,
        limit_max_requests=limit_max_requests,
        timeout_keep_alive=timeout_keep_alive,
        timeout_graceful_shutdown=timeout_graceful_shutdown,
        backlog=2048,
        workers=workers,
        loop="auto",
        h11_max_incomplete_event_size=16384,
    )

    uvicorn_server = uvicorn.Server(config)

    async def shutdown_handler() -> None:
        """Handle graceful shutdown of HTTP server."""
        logger.debug("Initiating HTTP server shutdown...")
        if hasattr(server, "_daemon") and server._daemon is not None:
            try:
                server._daemon.graceful_shutdown(timeout=timeout_graceful_shutdown)
            except Exception as e:
                logger.warning(f"Error during daemon shutdown: {e}")

    try:
        await uvicorn_server.serve()
    except (KeyboardInterrupt, SystemExit):
        logger.debug("Received shutdown signal")
        await shutdown_handler()
    finally:
        logger.debug("HTTP server stopped")
