"""
HTTP server for Gobby daemon.

Provides a FastAPI-based HTTP server for REST endpoints, MCP tool proxying,
and session management. Local-first version: no platform auth, no remote sync.
"""

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from gobby.adapters.codex import CodexAdapter
from gobby.hooks.broadcaster import HookEventBroadcaster
from gobby.hooks.hook_manager import HookManager
from gobby.llm import LLMService, create_llm_service
from gobby.mcp_proxy.server import create_mcp_server
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.sync.tasks import TaskSyncManager
from gobby.utils.metrics import Counter, get_metrics_collector
from gobby.utils.version import get_version
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SessionRegisterRequest(BaseModel):
    """Request model for session registration endpoint."""

    external_id: str = Field(
        ..., description="External session identifier (e.g., from Claude Code)"
    )
    machine_id: str | None = Field(None, description="Unique machine identifier")

    # Session metadata
    jsonl_path: str | None = Field(None, description="Path to JSONL transcript file")
    title: str | None = Field(None, description="Natural language session summary/title")
    source: str | None = Field(
        None, description="Session source (e.g., 'Claude Code', 'Agent SDK')"
    )
    parent_session_id: str | None = Field(
        None, description="Parent session ID for session lineage tracking"
    )
    status: str | None = Field(None, description="Session status (active, paused, etc.)")
    project_id: str | None = Field(None, description="Project ID to associate with session")
    project_path: str | None = Field(
        None, description="Project root directory path (for git extraction)"
    )
    git_branch: str | None = Field(None, description="Current git branch name")
    cwd: str | None = Field(None, description="Current working directory")


class HTTPServer:
    """
    FastAPI HTTP server for Gobby daemon.

    Handles MCP tool proxying, session management, and admin endpoints.
    Local-first version: no platform authentication, uses local SQLite storage.
    """

    def __init__(
        self,
        port: int = 8000,
        test_mode: bool = False,
        mcp_manager: Any | None = None,
        config: Any | None = None,
        codex_client: Any | None = None,
        session_manager: LocalSessionManager | None = None,
        websocket_server: Any | None = None,
        task_manager: LocalTaskManager | None = None,
        task_sync_manager: TaskSyncManager | None = None,
        message_processor: Any | None = None,
    ) -> None:
        """
        Initialize HTTP server.

        Args:
            port: Server port
            test_mode: Run in test mode (disable features that conflict with testing)
            mcp_manager: MCPClientManager instance for multi-server support
            config: DaemonConfig instance for configuration
            codex_client: CodexAppServerClient instance for Codex integration
            session_manager: LocalSessionManager for session storage
            websocket_server: Optional WebSocketServer instance for event broadcasting
            task_manager: LocalTaskManager instance
            task_sync_manager: TaskSyncManager instance
            message_processor: SessionMessageProcessor instance
        """
        self.port = port
        self.test_mode = test_mode
        self.mcp_manager = mcp_manager
        self.config = config
        self.codex_client = codex_client
        self.session_manager = session_manager
        self.task_manager = task_manager
        self.task_sync_manager = task_sync_manager
        self.message_processor = message_processor
        self.websocket_server = websocket_server

        # Initialize WebSocket broadcaster
        # Note: websocket_server might be None if disabled
        self.broadcaster = HookEventBroadcaster(websocket_server, config)

        self._start_time: float = time.time()

        # Create LLM service for multi-provider support
        self.llm_service: LLMService | None = None
        if config:
            try:
                self.llm_service = create_llm_service(config)
                logger.debug(
                    f"LLM service initialized with providers: {self.llm_service.enabled_providers}"
                )
            except Exception as e:
                logger.error(f"Failed to initialize LLM service: {e}")

        # Create MCP server instance
        self._mcp_server = None
        if mcp_manager:
            self._mcp_server = create_mcp_server(
                mcp_manager=mcp_manager,
                daemon_port=port,
                start_time=self._start_time,
                config=config,
                llm_service=self.llm_service,
                codex_client=codex_client,
                task_manager=task_manager,
                task_sync_manager=task_sync_manager,
            )
            logger.debug("MCP server initialized and will be mounted at /mcp")

        self.app = self._create_app()
        self._running = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._metrics = get_metrics_collector()
        self._daemon: Any = None  # Set externally by daemon

    def _resolve_project_id(self, project_id: str | None, cwd: str | None) -> str:
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

    def _create_app(self) -> FastAPI:
        """
        Create and configure FastAPI application.

        Returns:
            Configured FastAPI app instance
        """

        # Create MCP app first if available (needed for lifespan)
        mcp_app = None
        if self._mcp_server:
            mcp_app = self._mcp_server.http_app(
                path="/", transport="http", stateless_http=True, json_response=True
            )
            logger.debug("MCP HTTP app created")

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
            """Handle application startup and shutdown with combined lifespans."""
            logger.debug("Starting Gobby HTTP server on port %d", self.port)
            self._running = True
            self._start_time = time.time()

            # Startup operations
            if self.test_mode:
                logger.debug("Running in test mode - external connections disabled")

            # Initialize HookManager singleton with logging config
            hook_manager_kwargs: dict[str, Any] = {
                "daemon_host": "localhost",
                "daemon_port": self.port,
                "llm_service": self.llm_service,
                "config": self.config,
                "broadcaster": self.broadcaster,
                "mcp_manager": self.mcp_manager,
                "message_processor": self.message_processor,
            }
            if self.config:
                # Pass full log file path from config
                hook_manager_kwargs["log_file"] = self.config.logging.hook_manager
                hook_manager_kwargs["log_max_bytes"] = self.config.logging.max_size_mb * 1024 * 1024
                hook_manager_kwargs["log_backup_count"] = self.config.logging.backup_count

            app.state.hook_manager = HookManager(**hook_manager_kwargs)
            logger.debug("HookManager initialized in daemon")

            # Initialize CodexAdapter for session tracking
            app.state.codex_adapter = None
            if self.codex_client and CodexAdapter.is_codex_available():
                codex_adapter = CodexAdapter(hook_manager=app.state.hook_manager)
                codex_adapter.attach_to_client(self.codex_client)
                app.state.codex_adapter = codex_adapter
                logger.debug("CodexAdapter attached to CodexAppServerClient")

                # Sync existing Codex sessions when client is connected
                if self.codex_client.is_connected:
                    try:
                        synced = await codex_adapter.sync_existing_sessions()
                        logger.debug(f"Synced {synced} existing Codex sessions")
                    except Exception as e:
                        logger.warning(f"Failed to sync existing Codex sessions: {e}")

            # If MCP app exists, wrap its lifespan
            if mcp_app is not None:
                # Use router.lifespan_context for stable FastMCP version
                async with mcp_app.router.lifespan_context(app):
                    logger.debug("MCP server lifespan initialized")
                    yield
                logger.debug("MCP server lifespan shutdown complete")
            else:
                yield

            # Shutdown operations
            logger.debug("Shutting down Gobby HTTP server")

            # Cleanup CodexAdapter
            if hasattr(app.state, "codex_adapter") and app.state.codex_adapter:
                app.state.codex_adapter.detach_from_client()
                logger.debug("CodexAdapter detached")

            # Cleanup HookManager
            if hasattr(app.state, "hook_manager"):
                app.state.hook_manager.shutdown()
                logger.debug("HookManager shutdown complete")

            self._running = False

        app = FastAPI(
            title="Gobby Daemon",
            description="Local-first HTTP server for MCP and session management",
            version=get_version(),
            lifespan=lifespan,
        )

        # Add CORS middleware for cross-origin requests
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],  # Allow all origins for local development
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Register exception handlers
        self._register_exception_handlers(app)

        # Register routes
        self._register_routes(app)

        # Mount MCP server if available
        if mcp_app is not None:
            app.mount("/mcp", mcp_app)
            logger.debug("MCP server mounted at /mcp")

        return app

    def _register_exception_handlers(self, app: FastAPI) -> None:
        """
        Register global exception handlers.

        All exceptions return 200 OK to prevent Claude Code hook failures.

        Args:
            app: FastAPI application instance
        """

        @app.exception_handler(Exception)
        async def global_exception_handler(
            request: Request,
            exc: Exception,
        ) -> JSONResponse:
            """Handle all uncaught exceptions."""
            logger.error(
                "Unhandled exception in HTTP server: %s",
                exc,
                exc_info=True,
                extra={
                    "path": request.url.path,
                    "method": request.method,
                    "client": request.client.host if request.client else None,
                },
            )

            # Return 200 OK to prevent hook failure
            return JSONResponse(
                status_code=200,
                content={
                    "status": "error",
                    "message": "Internal error occurred but request acknowledged",
                    "error_logged": True,
                },
            )

    def _register_routes(self, app: FastAPI) -> None:
        """
        Register HTTP routes.

        Args:
            app: FastAPI application instance
        """

        @app.get("/admin/status")
        async def status_check() -> dict[str, Any]:
            """
            Comprehensive status check endpoint.

            Returns detailed health status including daemon state, uptime,
            memory usage, background tasks, and connection statistics.
            """
            start_time = time.perf_counter()

            # Get server uptime
            uptime_seconds = None
            if self._start_time is not None:
                uptime_seconds = time.time() - self._start_time

            # Get daemon status if available
            daemon_status = None
            if self._daemon is not None:
                try:
                    daemon_status = self._daemon.status()
                except Exception as e:
                    logger.warning(f"Failed to get daemon status: {e}")

            # Get process metrics
            try:
                process = psutil.Process(os.getpid())
                memory_info = process.memory_info()
                cpu_percent = process.cpu_percent(interval=0)

                process_metrics = {
                    "memory_rss_mb": round(memory_info.rss / (1024 * 1024), 2),
                    "memory_vms_mb": round(memory_info.vms / (1024 * 1024), 2),
                    "cpu_percent": cpu_percent,
                    "num_threads": process.num_threads(),
                }
            except Exception as e:
                logger.warning(f"Failed to get process metrics: {e}")
                process_metrics = None

            # Get background task status
            background_tasks = {
                "active": len(self._background_tasks),
                "total": self._metrics._counters.get(
                    "background_tasks_total", Counter("", "")
                ).value,
                "completed": self._metrics._counters.get(
                    "background_tasks_completed_total", Counter("", "")
                ).value,
                "failed": self._metrics._counters.get(
                    "background_tasks_failed_total", Counter("", "")
                ).value,
            }

            # Get MCP server status - include ALL configured servers
            mcp_health = {}
            if self.mcp_manager is not None:
                try:
                    # Iterate over all configured servers, not just connected ones
                    for config in self.mcp_manager.server_configs:
                        health = self.mcp_manager.health.get(config.name)
                        is_connected = config.name in self.mcp_manager.connections
                        mcp_health[config.name] = {
                            "connected": is_connected,
                            "status": health.state.value
                            if health
                            else ("connected" if is_connected else "not_started"),
                            "enabled": config.enabled,
                            "transport": config.transport,
                            "health": health.health.value if health else None,
                            "consecutive_failures": health.consecutive_failures if health else 0,
                            "last_health_check": health.last_health_check.isoformat()
                            if health and health.last_health_check
                            else None,
                            "response_time_ms": health.response_time_ms if health else None,
                        }
                except Exception as e:
                    logger.warning(f"Failed to get MCP health: {e}")

            # Calculate response time
            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "healthy" if self._running else "degraded",
                "server": {
                    "port": self.port,
                    "test_mode": self.test_mode,
                    "running": self._running,
                    "uptime_seconds": uptime_seconds,
                },
                "daemon": daemon_status,
                "process": process_metrics,
                "background_tasks": background_tasks,
                "mcp_servers": mcp_health,
                "response_time_ms": response_time_ms,
            }

        @app.get("/admin/metrics")
        async def get_metrics() -> PlainTextResponse:
            """
            Prometheus-compatible metrics endpoint.

            Returns metrics in Prometheus text exposition format including:
            - HTTP request counts and durations
            - Background task metrics
            - Daemon health metrics
            """
            try:
                # Update daemon health metrics if available
                if self._daemon is not None:
                    try:
                        uptime = self._daemon.uptime
                        if uptime is not None:
                            self._metrics.set_gauge("daemon_uptime_seconds", uptime)

                        # Get process info for daemon
                        process = psutil.Process(os.getpid())
                        memory_info = process.memory_info()
                        self._metrics.set_gauge("daemon_memory_usage_bytes", float(memory_info.rss))

                        cpu_percent = process.cpu_percent(interval=0)
                        self._metrics.set_gauge("daemon_cpu_percent", cpu_percent)
                    except Exception as e:
                        logger.warning(f"Failed to update daemon metrics: {e}")

                # Update background task gauge
                self._metrics.set_gauge(
                    "background_tasks_active", float(len(self._background_tasks))
                )

                # Export in Prometheus format
                prometheus_output = self._metrics.export_prometheus()
                return PlainTextResponse(
                    content=prometheus_output, media_type="text/plain; version=0.0.4"
                )

            except Exception as e:
                logger.error(f"Failed to export metrics: {e}", exc_info=True)
                return PlainTextResponse(
                    content=f"# Error exporting metrics: {e}\n",
                    status_code=500,
                    media_type="text/plain",
                )

        @app.get("/admin/config")
        async def get_config() -> dict[str, Any]:
            """
            Get daemon configuration and version information.

            Returns:
                Configuration data including ports, features, and versions
            """
            start_time = time.perf_counter()
            self._metrics.inc_counter("http_requests_total")

            try:
                config_data = {
                    "server": {
                        "port": self.port,
                        "test_mode": self.test_mode,
                        "running": self._running,
                        "version": get_version(),
                    },
                    "features": {
                        "session_manager": self.session_manager is not None,
                        "mcp_manager": self.mcp_manager is not None,
                    },
                    "endpoints": {
                        "mcp": [
                            "/mcp/{server_name}/tools/{tool_name}",
                        ],
                        "sessions": [
                            "/sessions/register",
                            "/sessions/{id}",
                        ],
                        "admin": [
                            "/admin/status",
                            "/admin/metrics",
                            "/admin/config",
                            "/admin/shutdown",
                        ],
                    },
                }

                response_time_ms = (time.perf_counter() - start_time) * 1000

                return {
                    "status": "success",
                    "config": config_data,
                    "response_time_ms": response_time_ms,
                }

            except Exception as e:
                logger.error(f"Config retrieval error: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.post("/sessions/register")
        async def register_session(request_data: SessionRegisterRequest) -> dict[str, Any]:
            """
            Register session metadata in local storage.

            Args:
                request_data: Session registration parameters

            Returns:
                Registration confirmation with session ID
            """
            self._metrics.inc_counter("http_requests_total")
            self._metrics.inc_counter("session_registrations_total")

            try:
                if self.session_manager is None:
                    raise HTTPException(status_code=503, detail="Session manager not available")

                # Get machine_id from request or generate
                machine_id = request_data.machine_id
                if not machine_id:
                    from gobby.utils.machine_id import get_machine_id

                    machine_id = get_machine_id()

                # Extract git branch if project path exists but git_branch not provided
                git_branch = request_data.git_branch
                if request_data.project_path and not git_branch:
                    from gobby.utils.git import get_git_metadata

                    git_metadata = get_git_metadata(request_data.project_path)
                    if git_metadata.get("git_branch"):
                        git_branch = git_metadata["git_branch"]

                # Resolve project_id from cwd if not provided
                project_id = self._resolve_project_id(request_data.project_id, request_data.cwd)

                # Register session in local storage
                session = self.session_manager.register(
                    external_id=request_data.external_id,
                    machine_id=machine_id,
                    source=request_data.source or "Claude Code",
                    project_id=project_id,
                    jsonl_path=request_data.jsonl_path,
                    title=request_data.title,
                    git_branch=git_branch,
                    parent_session_id=request_data.parent_session_id,
                )

                return {
                    "status": "registered",
                    "external_id": request_data.external_id,
                    "id": session.id,
                    "machine_id": machine_id,
                }

            except HTTPException:
                self._metrics.inc_counter("http_requests_errors_total")
                raise

            except Exception as e:
                self._metrics.inc_counter("http_requests_errors_total")
                logger.error(f"Error registering session: {e}", exc_info=True)
                raise HTTPException(
                    status_code=500, detail=f"Internal error during session registration: {e}"
                ) from e

        @app.get("/sessions/{session_id}")
        async def sessions_get(session_id: str) -> dict[str, Any]:
            """
            Get session by ID from local storage.

            Args:
                session_id: Session ID (UUID)

            Returns:
                Session data
            """
            start_time = time.perf_counter()
            self._metrics.inc_counter("http_requests_total")

            try:
                if self.session_manager is None:
                    raise HTTPException(status_code=503, detail="Session manager not available")

                session = self.session_manager.get(session_id)

                if session is None:
                    raise HTTPException(status_code=404, detail="Session not found")

                response_time_ms = (time.perf_counter() - start_time) * 1000

                return {
                    "status": "success",
                    "session": session.to_dict(),
                    "response_time_ms": response_time_ms,
                }

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Sessions get error: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.post("/sessions/find_current")
        async def find_current_session(request: Request) -> dict[str, Any]:
            """
            Find current active session by composite key.

            Uses composite key: external_id, machine_id, source
            """
            try:
                if self.session_manager is None:
                    raise HTTPException(status_code=503, detail="Session manager not available")

                body = await request.json()
                external_id = body.get("external_id")
                machine_id = body.get("machine_id")
                source = body.get("source")

                if not external_id or not machine_id or not source:
                    raise HTTPException(
                        status_code=400,
                        detail="Required fields: external_id, machine_id, source",
                    )

                session = self.session_manager.find_current(external_id, machine_id, source)

                if session is None:
                    return {"session": None}

                return {"session": session.to_dict()}

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Find current session error: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.post("/sessions/find_parent")
        async def find_parent_session(request: Request) -> dict[str, Any]:
            """
            Find parent session for handoff.

            Looks for most recent session in same project with handoff_ready status.
            Accepts either project_id directly or cwd (which is resolved to project_id).
            """
            try:
                if self.session_manager is None:
                    raise HTTPException(status_code=503, detail="Session manager not available")

                body = await request.json()
                machine_id = body.get("machine_id")
                source = body.get("source")
                project_id = body.get("project_id")
                cwd = body.get("cwd")

                if not source:
                    raise HTTPException(status_code=400, detail="Required field: source")

                if not machine_id:
                    from gobby.utils.machine_id import get_machine_id

                    machine_id = get_machine_id()

                # Resolve project_id from cwd if not provided
                if not project_id:
                    if not cwd:
                        raise HTTPException(
                            status_code=400,
                            detail="Required field: project_id or cwd",
                        )
                    project_id = self._resolve_project_id(None, cwd)

                session = self.session_manager.find_parent(
                    machine_id=machine_id,
                    source=source,
                    project_id=project_id,
                )

                if session is None:
                    return {"session": None}

                return {"session": session.to_dict()}

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Find parent session error: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.post("/sessions/update_status")
        async def update_session_status(request: Request) -> dict[str, Any]:
            """
            Update session status.
            """
            try:
                if self.session_manager is None:
                    raise HTTPException(status_code=503, detail="Session manager not available")

                body = await request.json()
                session_id = body.get("session_id")
                status = body.get("status")

                if not session_id or not status:
                    raise HTTPException(
                        status_code=400, detail="Required fields: session_id, status"
                    )

                session = self.session_manager.update_status(session_id, status)

                if session is None:
                    raise HTTPException(status_code=404, detail="Session not found")

                return {"session": session.to_dict()}

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Update session status error: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.post("/sessions/update_summary")
        async def update_session_summary(request: Request) -> dict[str, Any]:
            """
            Update session summary path.
            """
            try:
                if self.session_manager is None:
                    raise HTTPException(status_code=503, detail="Session manager not available")

                body = await request.json()
                session_id = body.get("session_id")
                summary_path = body.get("summary_path")

                if not session_id or not summary_path:
                    raise HTTPException(
                        status_code=400, detail="Required fields: session_id, summary_path"
                    )

                session = self.session_manager.update_summary(session_id, summary_path)

                if session is None:
                    raise HTTPException(status_code=404, detail="Session not found")

                return {"session": session.to_dict()}

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Update session summary error: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.get("/mcp/{server_name}/tools")
        async def list_mcp_tools(server_name: str) -> dict[str, Any]:
            """
            List available tools from an MCP server.

            Args:
                server_name: Name of the MCP server (e.g., "supabase", "context7")

            Returns:
                List of available tools with their descriptions
            """
            start_time = time.perf_counter()
            self._metrics.inc_counter("http_requests_total")

            try:
                if self.mcp_manager is None:
                    raise HTTPException(status_code=503, detail="MCP manager not available")

                # Get connection for this MCP server
                try:
                    connection = self.mcp_manager.get_client(server_name)
                except ValueError as e:
                    raise HTTPException(status_code=404, detail=str(e)) from e

                # Check if connection is active
                if not connection.is_connected or not connection._session:
                    # Try to reconnect
                    try:
                        await connection.connect()
                        logger.debug(f"Reconnected to '{server_name}' for tool listing")
                    except Exception as e:
                        raise HTTPException(
                            status_code=503,
                            detail=f"MCP server '{server_name}' is not connected: {e}",
                        ) from e

                # List tools using MCP SDK
                try:
                    tools_result = await connection._session.list_tools()
                    tools = []
                    for tool in tools_result.tools:
                        tool_dict = {
                            "name": tool.name,
                            "description": tool.description
                            if hasattr(tool, "description")
                            else None,
                        }

                        # Handle inputSchema
                        if hasattr(tool, "inputSchema"):
                            schema = tool.inputSchema
                            if hasattr(schema, "model_dump"):
                                tool_dict["inputSchema"] = schema.model_dump()
                            elif isinstance(schema, dict):
                                tool_dict["inputSchema"] = schema
                            else:
                                tool_dict["inputSchema"] = None
                        else:
                            tool_dict["inputSchema"] = None

                        tools.append(tool_dict)

                    response_time_ms = (time.perf_counter() - start_time) * 1000

                    logger.debug(
                        f"Listed {len(tools)} tools from {server_name}",
                        extra={
                            "server": server_name,
                            "tool_count": len(tools),
                            "response_time_ms": response_time_ms,
                        },
                    )

                    return {
                        "status": "success",
                        "server": server_name,
                        "tools": tools,
                        "tool_count": len(tools),
                        "response_time_ms": response_time_ms,
                    }

                except Exception as e:
                    logger.error(
                        f"Failed to list tools from {server_name}: {e}",
                        exc_info=True,
                        extra={"server": server_name},
                    )
                    raise HTTPException(status_code=500, detail=f"Failed to list tools: {e}") from e

            except HTTPException:
                raise
            except Exception as e:
                self._metrics.inc_counter("http_requests_errors_total")
                logger.error(f"MCP list tools error: {server_name}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.post("/mcp/{server_name}/tools/{tool_name}")
        async def mcp_proxy(server_name: str, tool_name: str, request: Request) -> dict[str, Any]:
            """
            Unified MCP proxy endpoint for calling MCP server tools.

            Args:
                server_name: Name of the MCP server
                tool_name: Name of the tool to call
                request: FastAPI request with tool arguments in body

            Returns:
                Tool execution result
            """
            start_time = time.perf_counter()
            self._metrics.inc_counter("http_requests_total")
            self._metrics.inc_counter("mcp_tool_calls_total")

            try:
                # Parse request body as tool arguments
                args = await request.json()

                if self.mcp_manager is None:
                    raise HTTPException(status_code=503, detail="MCP manager not available")

                # Call MCP tool
                try:
                    result = await self.mcp_manager.call_tool(server_name, tool_name, args)

                    response_time_ms = (time.perf_counter() - start_time) * 1000

                    logger.debug(
                        f"MCP tool call successful: {server_name}.{tool_name}",
                        extra={
                            "server": server_name,
                            "tool": tool_name,
                            "response_time_ms": response_time_ms,
                        },
                    )

                    self._metrics.inc_counter("mcp_tool_calls_succeeded_total")

                    return {
                        "status": "success",
                        "result": result,
                        "response_time_ms": response_time_ms,
                    }

                except ValueError as e:
                    self._metrics.inc_counter("mcp_tool_calls_failed_total")
                    logger.warning(
                        f"MCP tool not found: {server_name}.{tool_name}",
                        extra={"server": server_name, "tool": tool_name, "error": str(e)},
                    )
                    raise HTTPException(status_code=404, detail=str(e)) from e
                except Exception as e:
                    self._metrics.inc_counter("mcp_tool_calls_failed_total")
                    logger.error(
                        f"MCP tool call error: {server_name}.{tool_name}",
                        exc_info=True,
                        extra={"server": server_name, "tool": tool_name},
                    )
                    raise HTTPException(status_code=500, detail=str(e)) from e

            except HTTPException:
                raise
            except Exception as e:
                self._metrics.inc_counter("mcp_tool_calls_failed_total")
                logger.error(f"MCP proxy error: {server_name}.{tool_name}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e)) from e

        @app.post("/admin/shutdown")
        async def shutdown() -> dict[str, Any]:
            """
            Graceful daemon shutdown endpoint.

            Returns:
                Shutdown confirmation
            """
            start_time = time.perf_counter()

            self._metrics.inc_counter("http_requests_total")
            self._metrics.inc_counter("shutdown_requests_total")

            try:
                logger.debug("Shutdown requested via HTTP endpoint")

                # Create background task for shutdown
                task = asyncio.create_task(self._process_shutdown())

                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

                response_time_ms = (time.perf_counter() - start_time) * 1000

                return {
                    "status": "shutting_down",
                    "message": "Graceful shutdown initiated",
                    "response_time_ms": response_time_ms,
                }

            except Exception as e:
                self._metrics.inc_counter("http_requests_errors_total")
                logger.error("Error initiating shutdown: %s", e, exc_info=True)
                return {
                    "status": "error",
                    "message": "Shutdown failed to initiate",
                }

        @app.post("/hooks/execute")
        async def execute_hook(request: Request) -> dict[str, Any]:
            """
            Execute CLI hook via adapter pattern.

            Request body:
                {
                    "hook_type": "session-start",
                    "input_data": {...},
                    "source": "claude"
                }

            Returns:
                Hook execution result with status
            """
            start_time = time.perf_counter()
            self._metrics.inc_counter("http_requests_total")
            self._metrics.inc_counter("hooks_total")

            try:
                # Parse request
                payload = await request.json()
                hook_type = payload.get("hook_type")
                source = payload.get("source")

                if not hook_type:
                    raise HTTPException(status_code=400, detail="hook_type required")

                if not source:
                    raise HTTPException(status_code=400, detail="source required")

                # Get HookManager from app.state
                if not hasattr(app.state, "hook_manager"):
                    raise HTTPException(status_code=503, detail="HookManager not initialized")

                hook_manager = app.state.hook_manager

                # Select adapter based on source
                from gobby.adapters.claude_code import ClaudeCodeAdapter
                from gobby.adapters.codex import CodexNotifyAdapter
                from gobby.adapters.gemini import GeminiAdapter

                if source == "claude":
                    adapter = ClaudeCodeAdapter(hook_manager=hook_manager)
                elif source == "gemini":
                    adapter = GeminiAdapter(hook_manager=hook_manager)
                elif source == "codex":
                    adapter = CodexNotifyAdapter(hook_manager=hook_manager)
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unsupported source: {source}. Supported: claude, gemini, codex",
                    )

                # Execute hook via adapter
                try:
                    result = await asyncio.to_thread(adapter.handle_native, payload, hook_manager)

                    response_time_ms = (time.perf_counter() - start_time) * 1000
                    self._metrics.inc_counter("hooks_succeeded_total")

                    logger.debug(
                        f"Hook executed: {hook_type}",
                        extra={
                            "hook_type": hook_type,
                            "continue": result.get("continue"),
                            "response_time_ms": response_time_ms,
                        },
                    )

                    return cast(dict[str, Any], result)

                except ValueError as e:
                    self._metrics.inc_counter("hooks_failed_total")
                    logger.warning(
                        f"Invalid hook request: {hook_type}",
                        extra={"hook_type": hook_type, "error": str(e)},
                    )
                    raise HTTPException(status_code=400, detail=str(e)) from e

                except Exception as e:
                    self._metrics.inc_counter("hooks_failed_total")
                    logger.error(
                        f"Hook execution failed: {hook_type}",
                        exc_info=True,
                        extra={"hook_type": hook_type},
                    )
                    raise HTTPException(status_code=500, detail=str(e)) from e

            except HTTPException:
                raise
            except Exception as e:
                self._metrics.inc_counter("hooks_failed_total")
                logger.error("Hook endpoint error", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e)) from e

    async def _process_shutdown(self) -> None:
        """
        Background task to perform graceful daemon shutdown.
        """
        start_time = time.perf_counter()

        try:
            logger.debug("Processing graceful shutdown")

            # Wait for pending background tasks to complete
            pending_tasks_count = len(self._background_tasks)
            if pending_tasks_count > 0:
                logger.debug(
                    "Waiting for pending background tasks to complete",
                    extra={"pending_tasks": pending_tasks_count},
                )

                max_wait = 30.0
                wait_start = time.perf_counter()

                while (
                    len(self._background_tasks) > 0
                    and (time.perf_counter() - wait_start) < max_wait
                ):
                    await asyncio.sleep(0.5)

                completed_wait = time.perf_counter() - wait_start
                remaining_tasks = len(self._background_tasks)

                if remaining_tasks > 0:
                    logger.warning(
                        "Shutdown timeout - some background tasks still pending",
                        extra={
                            "remaining_tasks": remaining_tasks,
                            "wait_seconds": completed_wait,
                        },
                    )
                else:
                    logger.debug(
                        "All background tasks completed",
                        extra={"wait_seconds": completed_wait},
                    )

            # Disconnect all MCP servers
            if self.mcp_manager:
                logger.debug("Disconnecting MCP servers...")
                try:
                    await self.mcp_manager.disconnect_all()
                    logger.debug("MCP servers disconnected")
                except Exception as e:
                    logger.warning(f"Error disconnecting MCP servers: {e}")

            duration_seconds = time.perf_counter() - start_time
            self._metrics.inc_counter("shutdown_succeeded_total")

            logger.debug(
                "Shutdown processed",
                extra={"duration_seconds": duration_seconds},
            )

        except Exception as e:
            duration_seconds = time.perf_counter() - start_time
            self._metrics.inc_counter("shutdown_failed_total")

            logger.error(
                "Shutdown processing failed: %s",
                e,
                exc_info=True,
                extra={"duration_seconds": duration_seconds},
            )


async def create_server(
    port: int = 8765,
    test_mode: bool = False,
    mcp_manager: Any | None = None,
    config: Any | None = None,
    session_manager: LocalSessionManager | None = None,
) -> HTTPServer:
    """
    Create HTTP server instance.

    Args:
        port: Port to listen on
        test_mode: Enable test mode
        mcp_manager: MCP client manager
        config: Daemon configuration
        session_manager: Local session manager

    Returns:
        Configured HTTPServer instance
    """
    return HTTPServer(
        port=port,
        test_mode=test_mode,
        mcp_manager=mcp_manager,
        config=config,
        session_manager=session_manager,
    )


async def run_server(
    server: HTTPServer,
    host: str = "0.0.0.0",
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
