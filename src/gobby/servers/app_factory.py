"""
FastAPI application factory for the Gobby HTTP server.

Creates and configures the FastAPI app with lifespan management,
middleware, route registration, and static file mounts.
"""

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.hooks.hook_manager import HookManager
from gobby.servers.exception_handlers import register_exception_handlers
from gobby.utils.version import get_version

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

    class _ProductionUIServices(Protocol):
        @property
        def config(self) -> Any: ...

    class _ProductionUIServer(Protocol):
        @property
        def services(self) -> _ProductionUIServices: ...


logger = logging.getLogger(__name__)
_CODEX_SYNC_TIMEOUT_SECONDS = 10.0


def create_app(server: "HTTPServer") -> FastAPI:
    """
    Create and configure FastAPI application.

    Args:
        server: HTTPServer instance that owns this app

    Returns:
        Configured FastAPI app instance
    """

    # Create MCP app first if available (needed for lifespan)
    mcp_app = None
    if server._mcp_server:
        mcp_app = server._mcp_server.streamable_http_app()
        logger.debug("MCP HTTP app created")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Handle application startup and shutdown with combined lifespans."""
        logger.debug(f"Starting Gobby HTTP server on port {server.port}")
        server._running = True
        server._start_time = time.time()

        # Startup operations
        if server.test_mode:
            logger.debug("Running in test mode - external connections disabled")

        # Initialize HookManager singleton with logging config
        hook_manager_kwargs: dict[str, Any] = {
            "daemon_host": "localhost",
            "daemon_port": server.port,
            "llm_service": server.services.llm_service,
            "config": server.services.config,
            "broadcaster": server.broadcaster,
            "tool_proxy_getter": lambda: server.tool_proxy,
            "message_processor": server.services.message_processor,
            "memory_sync_manager": server.services.memory_sync_manager,
            "task_sync_manager": server.services.task_sync_manager,
            "agent_runner": server.services.agent_runner,
            "completion_registry": server.services.completion_registry,
            "database": server.services.database,
            "session_manager": server.services.session_manager,
        }
        if server.services.agent_runner is not None:
            server.services.agent_runner.agent_lifecycle_monitor = (
                server.services.agent_lifecycle_monitor
            )

        # Create code index trigger for post-edit incremental indexing
        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is not None:
            try:
                from gobby.code_index.trigger import CodeIndexTrigger

                hook_manager_kwargs["code_index_trigger"] = CodeIndexTrigger(
                    loop=asyncio.get_running_loop(),
                    debounce_seconds=2.0,
                )
            except Exception as e:
                logger.warning(f"Failed to create CodeIndexTrigger: {e}")

        if server.services.config:
            # Pass full log file path from config
            hook_manager_kwargs["log_file"] = server.services.config.telemetry.log_file_hook_manager
            hook_manager_kwargs["log_max_bytes"] = (
                server.services.config.telemetry.max_size_mb * 1024 * 1024
            )
            hook_manager_kwargs["log_backup_count"] = server.services.config.telemetry.backup_count

            app.state.hook_manager = HookManager(**hook_manager_kwargs)
            server._hook_manager = app.state.hook_manager
        logger.debug("HookManager initialized in daemon")

        # Initialize PendingInteractionManager for web chat approval flows
        if server.services.database:
            from gobby.servers.pending_interactions import PendingInteractionManager

            app.state.pending_interaction_manager = PendingInteractionManager(
                server.services.database,
                run_db=server.run_db,
            )
            try:
                await app.state.pending_interaction_manager.expire_all_pending()
            except Exception as e:
                logger.warning(f"Failed to expire pending interactions on startup: {e}")
            logger.debug("PendingInteractionManager initialized")

        # Wire up stop_registry to WebSocket server for stop_request handling
        # Check both services container and direct attribute (runner sets both)
        ws_server = server.services.websocket_server or server.websocket_server
        if (
            ws_server
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_stop_registry")
        ):
            ws_server.stop_registry = app.state.hook_manager._stop_registry
            logger.debug("Stop registry connected to WebSocket server")

        # Wire workflow handler to WebSocket server for SDK lifecycle
        if (
            ws_server
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_workflow_handler")
        ):
            ws_server.workflow_handler = app.state.hook_manager._workflow_handler
            logger.debug("Workflow handler connected to WebSocket server")

        # Wire event handlers to WebSocket server for skill interception
        if (
            ws_server
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_event_handlers")
        ):
            ws_server.event_handlers = app.state.hook_manager._event_handlers
            logger.debug("Event handlers connected to WebSocket server")

        # Wire webhook dispatcher for blocking webhook parity with CLI path
        if (
            ws_server
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_webhook_dispatcher")
        ):
            ws_server.webhook_dispatcher = app.state.hook_manager._webhook_dispatcher
            logger.debug("Webhook dispatcher connected to WebSocket server")

        # Wire hook event broadcaster for audit trail parity with CLI path
        if ws_server and server.broadcaster:
            ws_server.hook_broadcaster = server.broadcaster
            logger.debug("Hook event broadcaster connected to WebSocket server")

        if server.session_manager is not None:
            listener_loop = asyncio.get_running_loop()

            def _broadcast_session_change(event: str, session_id: str) -> None:
                if not ws_server or listener_loop.is_closed():
                    return

                def _schedule() -> None:
                    listener_loop.create_task(ws_server.broadcast_session_event(event, session_id))

                listener_loop.call_soon_threadsafe(_schedule)

            server.session_manager.register_session_change_listener(_broadcast_session_change)
            app.state.session_change_listener = _broadcast_session_change
            logger.debug("Session change listener connected to session manager")

        # Wire inter-session message manager for message piggyback delivery
        if (
            ws_server
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_inter_session_msg_manager")
            and app.state.hook_manager._inter_session_msg_manager
        ):
            ws_server.inter_session_msg_manager = app.state.hook_manager._inter_session_msg_manager
            logger.debug("Inter-session message manager connected to WebSocket server")

        # Voice models are loaded lazily on first mic-button click (voice_prepare)
        # to avoid ~4GB of idle memory from Chatterbox/Whisper at startup.

        # Wire workflow handler to AgentRunner for embedded agent hooks
        if (
            server.services.agent_runner
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_workflow_handler")
        ):
            server.services.agent_runner.workflow_handler = app.state.hook_manager._workflow_handler
            logger.debug("Workflow handler connected to AgentRunner")

        # Wire session_coordinator to lifecycle monitor for worktree cleanup
        if (
            hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_session_coordinator")
            and server.services.agent_lifecycle_monitor
        ):
            server.services.agent_lifecycle_monitor.set_session_coordinator(
                app.state.hook_manager._session_coordinator
            )
            logger.debug("Session coordinator connected to agent lifecycle monitor")

        # Wire completion_registry to session_coordinator for agent completion events
        if (
            hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_session_coordinator")
            and server.services.completion_registry
        ):
            app.state.hook_manager._session_coordinator.set_completion_registry(
                server.services.completion_registry
            )
            logger.debug("Completion registry connected to session coordinator")

        # Initialize canvas broadcaster
        from gobby.mcp_proxy.tools.canvas import set_broadcaster

        async def _canvas_broadcaster(**kwargs: Any) -> None:
            ws = server.services.websocket_server or server.websocket_server
            if ws and hasattr(ws, "broadcast_canvas_event"):
                await ws.broadcast_canvas_event(**kwargs)

        set_broadcaster(_canvas_broadcaster)
        logger.debug("Canvas broadcaster connected to WebSocket server")

        # Initialize artifact broadcaster
        from gobby.mcp_proxy.tools.canvas import set_artifact_broadcaster

        async def _artifact_broadcaster(**kwargs: Any) -> None:
            ws = server.services.websocket_server or server.websocket_server
            if ws and hasattr(ws, "broadcast_artifact_event"):
                await ws.broadcast_artifact_event(**kwargs)

        set_artifact_broadcaster(_artifact_broadcaster)
        logger.debug("Artifact broadcaster connected to WebSocket server")

        # Store server instance for dependency injection
        app.state.server = server

        runtime_manager = getattr(server.services, "web_chat_runtime_manager", None)
        if runtime_manager is not None:
            try:
                await runtime_manager.start(background=True)
                logger.debug("Web chat runtime manager startup scheduled")
            except Exception as e:
                logger.warning(f"Failed to start web chat runtime manager: {e}")

        async def _sync_existing_codex_sessions() -> None:
            if not getattr(app.state, "codex_adapter", None):
                return
            try:
                synced = await asyncio.wait_for(
                    app.state.codex_adapter.sync_existing_sessions(),
                    timeout=_CODEX_SYNC_TIMEOUT_SECONDS,
                )
                logger.debug(f"Synced {synced} existing Codex sessions")
            except TimeoutError:
                logger.warning(
                    "Timed out syncing existing Codex sessions after %.1fs",
                    _CODEX_SYNC_TIMEOUT_SECONDS,
                )
            except Exception as e:
                logger.warning(f"Failed to sync existing Codex sessions: {e}")

        # Initialize CodexAdapter for session tracking
        app.state.codex_adapter = None
        app.state.codex_sync_task = None
        if server.codex_client and CodexAdapter.is_codex_available():
            codex_adapter = CodexAdapter(hook_manager=app.state.hook_manager)
            codex_adapter.attach_to_client(server.codex_client)
            app.state.codex_adapter = codex_adapter
            logger.debug("CodexAdapter attached to CodexAppServerClient")

            # Sync existing Codex sessions after startup without blocking HTTP
            if server.codex_client.is_connected:
                app.state.codex_sync_task = asyncio.create_task(_sync_existing_codex_sessions())

        # Start TmuxPaneMonitor if tmux is enabled
        if server.services.config and server.services.config.tmux.enabled:
            try:
                from gobby.agents.tmux import set_tmux_pane_monitor
                from gobby.agents.tmux.pane_monitor import TmuxPaneMonitor

                monitor = TmuxPaneMonitor(
                    session_end_callback=app.state.hook_manager._event_handlers.handle_session_end,
                    config=server.services.config.tmux,
                    session_manager=app.state.hook_manager._session_manager,
                )
                set_tmux_pane_monitor(monitor)
                await monitor.start()
                logger.debug("TmuxPaneMonitor started")
            except Exception as e:
                logger.warning(f"Failed to start TmuxPaneMonitor: {e}")

        # Start SessionLivenessMonitor (detects dead CLI sessions via PID checks)
        try:
            from gobby.sessions.liveness_monitor import SessionLivenessMonitor

            session_storage = app.state.hook_manager._session_manager
            liveness_monitor = SessionLivenessMonitor(
                session_storage=session_storage,
                dispatch_summaries_fn=getattr(
                    app.state.hook_manager, "_dispatch_session_summaries", None
                ),
                message_processor=getattr(app.state.hook_manager, "_message_processor", None),
            )
            app.state.liveness_monitor = liveness_monitor
            await liveness_monitor.start()
            logger.debug("SessionLivenessMonitor started")
        except Exception as e:
            logger.warning(f"Failed to start SessionLivenessMonitor: {e}")

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

        if hasattr(app.state, "session_change_listener") and server.session_manager is not None:
            server.session_manager.unregister_session_change_listener(
                app.state.session_change_listener
            )
            del app.state.session_change_listener
            logger.debug("Session change listener disconnected from session manager")

        voice_cleanup = getattr(ws_server, "cleanup_voice", None) if ws_server else None
        if voice_cleanup:
            try:
                cleanup_result = voice_cleanup()
                if inspect.isawaitable(cleanup_result):
                    await cleanup_result
                logger.debug("Voice resources cleaned up")
            except Exception as e:
                logger.warning(f"Failed to clean up voice resources: {e}")

        # Cleanup CodexAdapter and stop app-server client
        if getattr(app.state, "codex_sync_task", None):
            app.state.codex_sync_task.cancel()
            try:
                await app.state.codex_sync_task
            except asyncio.CancelledError:
                pass
            logger.debug("Codex session sync task stopped")
        if hasattr(app.state, "codex_adapter") and app.state.codex_adapter:
            app.state.codex_adapter.detach_from_client()
            logger.debug("CodexAdapter detached")
        if runtime_manager is not None:
            try:
                await runtime_manager.stop()
                logger.debug("Web chat runtime manager stopped")
            except Exception as e:
                logger.warning(f"Failed to stop web chat runtime manager: {e}")

        # Stop SessionLivenessMonitor
        if hasattr(app.state, "liveness_monitor") and app.state.liveness_monitor:
            try:
                await app.state.liveness_monitor.stop()
                app.state.liveness_monitor = None
                logger.debug("SessionLivenessMonitor stopped")
            except Exception as e:
                logger.warning(f"Failed to stop SessionLivenessMonitor: {e}")

        # Stop TmuxPaneMonitor
        try:
            from gobby.agents.tmux import get_tmux_pane_monitor, set_tmux_pane_monitor

            pane_monitor = get_tmux_pane_monitor()
            if pane_monitor:
                await pane_monitor.stop()
                set_tmux_pane_monitor(None)
                logger.debug("TmuxPaneMonitor stopped")
        except Exception as e:
            logger.warning(f"Failed to stop TmuxPaneMonitor: {e}")

        # Cleanup PendingInteractionManager
        if hasattr(app.state, "pending_interaction_manager"):
            try:
                await app.state.pending_interaction_manager.cleanup()
                logger.debug("PendingInteractionManager cleanup complete")
            except Exception as e:
                logger.exception("PendingInteractionManager cleanup failed: %s", e)

        # Cleanup HookManager
        if hasattr(app.state, "hook_manager"):
            app.state.hook_manager.shutdown()
            logger.debug("HookManager shutdown complete")

        # Process graceful shutdown (tasks, MCP connections)
        await server._process_shutdown()

        server._running = False

    app = FastAPI(
        title="Gobby Daemon",
        description="Local-first HTTP server for MCP and session management",
        version=get_version(),
        lifespan=lifespan,
    )

    # Add CORS middleware for cross-origin requests
    cors_origins = (
        ["*"]
        if server.services.config and server.services.config.test_mode
        else (
            server.services.config.cors_origins
            if server.services.config
            else ["http://localhost:*"]
        )
    )
    # Convert glob patterns (e.g. "http://localhost:*") to a single regex
    # because CORSMiddleware treats allow_origins as literal strings.
    import fnmatch

    origin_regex_parts = [fnmatch.translate(o) for o in cors_origins if "*" in o]
    exact_origins = [o for o in cors_origins if "*" not in o]
    origin_regex = "|".join(origin_regex_parts) if origin_regex_parts else None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=exact_origins if exact_origins else [],
        allow_origin_regex=origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add telemetry middleware (automatic request tracking)
    from gobby.telemetry.middleware import TelemetryMiddleware

    app.add_middleware(TelemetryMiddleware)

    # Add project context middleware (sets ContextVar from X-Gobby-Project-Id header)
    from gobby.servers.middleware.project_context import ProjectContextMiddleware

    app.add_middleware(ProjectContextMiddleware)

    # Add auth middleware (checks after CORS, before routes)
    from gobby.servers.middleware.auth import AuthMiddleware

    app.add_middleware(AuthMiddleware, server=server)

    # Register exception handlers
    register_exception_handlers(app)

    # Register routes
    _register_routes(app, server)

    # Mount MCP server if available
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
        logger.debug("MCP server mounted at /mcp")

    # Mount Canvas sandbox
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    canvas_dir = Path.home() / ".gobby" / "canvas"
    canvas_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/__gobby__/canvas", StaticFiles(directory=str(canvas_dir)), name="canvas-sandbox")
    logger.debug("Canvas sandbox mounted at /__gobby__/canvas")

    # Mount WebSocket proxy (before production UI catch-all)
    _mount_ws_proxy(app, server)

    # Mount static files for production UI mode
    if (
        server.services.config
        and server.services.config.ui.enabled
        and server.services.config.ui.mode == "production"
    ):
        _mount_production_ui(app, server)

    return app


def _register_routes(app: FastAPI, server: "HTTPServer") -> None:
    """
    Register HTTP routes using extracted router modules.

    Args:
        app: FastAPI application instance
        server: HTTPServer instance
    """
    from gobby.servers.routes import (
        create_admin_router,
        create_agent_spawn_router,
        create_agents_router,
        create_build_router,
        create_chat_router,
        create_code_index_router,
        create_communications_router,
        create_configuration_router,
        create_cron_router,
        create_files_router,
        create_github_triage_router,
        create_hooks_router,
        create_mcp_router,
        create_memory_router,
        create_metrics_router,
        create_pipelines_router,
        create_profiles_router,
        create_projects_router,
        create_providers_router,
        create_rules_router,
        create_sessions_router,
        create_skills_router,
        create_source_control_router,
        create_stages_router,
        create_tasks_router,
        create_traces_router,
        create_voice_router,
        create_webhooks_router,
        create_workflows_router,
    )
    from gobby.servers.routes.auth import create_auth_router

    # Include all routers
    app.include_router(create_auth_router(server))
    app.include_router(create_admin_router(server))
    app.include_router(create_agent_spawn_router(server))
    app.include_router(create_agents_router(server))
    app.include_router(create_build_router(server))
    app.include_router(create_chat_router(server))
    app.include_router(create_sessions_router(server))
    app.include_router(create_memory_router(server))
    app.include_router(create_tasks_router(server))
    app.include_router(create_stages_router(server))
    app.include_router(create_code_index_router(server))
    app.include_router(create_cron_router(server))
    app.include_router(create_mcp_router())
    app.include_router(create_hooks_router(server))
    app.include_router(create_webhooks_router())
    app.include_router(create_pipelines_router(server))
    app.include_router(create_files_router(server))
    app.include_router(create_github_triage_router(server))
    app.include_router(create_projects_router(server))
    app.include_router(create_profiles_router(server))
    app.include_router(create_providers_router(server))
    app.include_router(create_skills_router(server))
    app.include_router(create_voice_router(server))
    app.include_router(create_configuration_router(server))
    app.include_router(create_workflows_router(server))
    app.include_router(create_rules_router(server))
    app.include_router(create_source_control_router(server))
    app.include_router(create_traces_router(server))
    app.include_router(create_metrics_router(server))

    comms_config = getattr(server.services.config, "communications", None)
    if comms_config and comms_config.enabled:
        app.include_router(create_communications_router(server))


def _mount_ws_proxy(app: FastAPI, server: "HTTPServer") -> None:
    """Mount a WebSocket proxy that forwards /ws/* to the standalone WebSocket server.

    In production mode the frontend connects WebSocket to the HTTP server's
    host (window.location.host), but the actual WebSocket server runs on a
    separate port.  This proxy bridges the two so that clients only need to
    know about the HTTP port.
    """
    ws_port = 60888
    cfg = server.services.config
    if cfg and hasattr(cfg, "websocket") and cfg.websocket:
        ws_port = cfg.websocket.port

    @app.websocket("/ws/{path:path}")
    async def ws_proxy(websocket: WebSocket, path: str) -> None:
        await websocket.accept()

        # Build target URL preserving sub-path and query string
        query = str(websocket.query_params) if websocket.query_params else ""
        target = f"ws://localhost:{ws_port}/{path}"
        if query:
            target += f"?{query}"

        import websockets

        try:
            async with websockets.connect(target) as backend:

                async def client_to_backend() -> None:
                    try:
                        while True:
                            data = await websocket.receive_text()
                            await backend.send(data)
                    except WebSocketDisconnect:
                        await backend.close()

                async def backend_to_client() -> None:
                    try:
                        async for message in backend:
                            if isinstance(message, str):
                                await websocket.send_text(message)
                            else:
                                await websocket.send_bytes(message)
                    except websockets.exceptions.ConnectionClosed:
                        await websocket.close()

                await asyncio.gather(client_to_backend(), backend_to_client())
        except Exception as e:
            logger.debug(f"WebSocket proxy error: {e}")
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    # Also handle bare /ws (no trailing path)
    @app.websocket("/ws")
    async def ws_proxy_root(websocket: WebSocket) -> None:
        await ws_proxy(websocket, "")

    logger.debug(f"WebSocket proxy mounted at /ws -> localhost:{ws_port}")


def _mount_production_ui(app: FastAPI, server: "_ProductionUIServer") -> None:
    """Mount static files and SPA catch-all for production UI mode."""
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    from gobby.cli.utils import find_web_dir

    web_dir = find_web_dir(server.services.config)
    if not web_dir:
        logger.warning("UI enabled in production mode but web/ directory not found")
        return

    dist_dir = web_dir / "dist"
    if not dist_dir.exists():
        logger.warning(f"UI dist directory not found at {dist_dir}. Run 'gobby ui build' first.")
        return

    index_html = dist_dir / "index.html"
    if not index_html.exists():
        logger.warning(f"index.html not found in {dist_dir}")
        return

    # Mount /assets for Vite-built JS/CSS
    assets_dir = dist_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="ui-assets")

    # SPA catch-all: serve index.html for non-API paths
    @app.get("/{path:path}")
    async def spa_catch_all(request: Request, path: str) -> FileResponse:
        # Don't intercept API, admin, MCP, or WebSocket paths
        # Normalize with trailing slash so both "api" and "api/..." are excluded
        path_check = path if path.endswith("/") else path + "/"
        if path_check.startswith(("api/", "ws/", "health/")):
            raise HTTPException(status_code=404)
        # Serve static file if it exists
        static_file = dist_dir / path
        # Prevent path traversal attacks
        try:
            static_file = static_file.resolve()
            if not static_file.is_relative_to(dist_dir.resolve()):
                raise HTTPException(status_code=404)
        except (ValueError, OSError):
            raise HTTPException(status_code=404) from None
        if path and static_file.exists() and static_file.is_file():
            return FileResponse(str(static_file))
        # Fallback to index.html for SPA routing
        return FileResponse(str(index_html))

    logger.info(f"Production UI mounted from {dist_dir}")
