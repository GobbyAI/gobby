"""Daemon FastAPI lifespan startup and shutdown wiring."""

import asyncio
import inspect
import logging
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


logger = logging.getLogger("gobby.servers.app_factory")
_CODEX_SYNC_TIMEOUT_SECONDS = 10.0


def create_lifespan(
    server: "HTTPServer",
    mcp_app: Any | None,
    *,
    hook_manager_factory_getter: Callable[[], Callable[..., Any]],
    codex_adapter_cls_getter: Callable[[], Any],
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build the FastAPI lifespan handler for the daemon server."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Handle application startup and shutdown with combined lifespans."""
        logger.debug("Starting Gobby HTTP server on port %s", server.port)
        server._running = True
        server._start_time = time.time()

        if server.test_mode:
            logger.debug("Running in test mode - external connections disabled")

        config = server.resolve_runtime_config()
        hook_manager_kwargs: dict[str, Any] = {
            "daemon_host": "localhost",
            "daemon_port": server.port,
            "llm_service": server.services.llm_service,
            "config": config,
            "broadcaster": server.broadcaster,
            "tool_proxy_getter": lambda: server.tool_proxy,
            "message_processor_resolver": lambda: server.message_processor,
            "agent_runner": server.services.agent_runner,
            "completion_registry": server.services.completion_registry,
            "config_runtime": server.services.config_runtime,
            "database": server.services.database,
            "session_manager": server.services.session_manager,
            "memory_manager": server.services.memory_manager,
        }
        if (
            server.services.agent_runner is not None
            and server.services.agent_lifecycle_monitor is not None
        ):
            server.services.agent_runner.agent_lifecycle_monitor = (
                server.services.agent_lifecycle_monitor
            )

        code_indexer = getattr(server.services, "code_indexer", None)
        if code_indexer is not None:
            try:
                from gobby.code_index.trigger import CodeIndexTrigger

                gcode_gateway = code_indexer.gcode_gateway
                if gcode_gateway is not None:
                    hook_manager_kwargs["code_index_trigger"] = CodeIndexTrigger(
                        loop=asyncio.get_running_loop(),
                        gcode_gateway=gcode_gateway,
                        daemon_config_breaker=code_indexer.daemon_config_breaker,
                        launch_factory=getattr(code_indexer, "launch_factory", None),
                        launch_source=code_indexer,
                    )
                else:
                    logger.warning(
                        "CodeIndexTrigger unavailable because the gcode gateway is not configured"
                    )
            except Exception as e:
                logger.warning("Failed to create CodeIndexTrigger: %s", e)

        if not getattr(app.state, "hook_manager", None):
            app.state.hook_manager = hook_manager_factory_getter()(**hook_manager_kwargs)
            logger.debug("HookManager initialized in daemon")
        else:
            logger.debug("Reusing preconfigured HookManager in daemon")
        server._hook_manager = app.state.hook_manager
        app.state.hook_manager.event_handlers.set_attention_metadata_store(
            server.services.attention_metadata_store
        )
        message_processor = server.message_processor
        if message_processor is not None:
            message_processor.set_hook_manager(app.state.hook_manager)

        if server.services.database:
            from gobby.servers.pending_interactions import PendingInteractionManager

            app.state.pending_interaction_manager = PendingInteractionManager(
                server.services.database,
                run_db=server.run_db,
            )
            try:
                await app.state.pending_interaction_manager.expire_all_pending()
            except Exception as e:
                logger.warning("Failed to expire pending interactions on startup: %s", e)
            logger.debug("PendingInteractionManager initialized")

        ws_server = server.services.websocket_server or server.websocket_server
        if (
            ws_server
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_stop_registry")
        ):
            ws_server.stop_registry = app.state.hook_manager._stop_registry
            logger.debug("Stop registry connected to WebSocket server")

        if (
            ws_server
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_workflow_handler")
        ):
            ws_server.workflow_handler = app.state.hook_manager._workflow_handler
            logger.debug("Workflow handler connected to WebSocket server")

        if (
            ws_server
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "event_handlers")
        ):
            ws_server.event_handlers = app.state.hook_manager.event_handlers
            logger.debug("Event handlers connected to WebSocket server")

        if (
            ws_server
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_webhook_dispatcher")
        ):
            ws_server.webhook_dispatcher = app.state.hook_manager._webhook_dispatcher
            logger.debug("Webhook dispatcher connected to WebSocket server")

        if ws_server and server.broadcaster:
            ws_server.hook_broadcaster = server.broadcaster
            logger.debug("Hook event broadcaster connected to WebSocket server")

        if server.session_manager is not None:
            listener_loop = asyncio.get_running_loop()
            session_broadcast_tasks: set[asyncio.Task[Any]] = set()
            app.state.session_broadcast_closed = False
            app.state.session_broadcast_tasks = session_broadcast_tasks

            def _session_broadcast_done(done_task: asyncio.Task[Any]) -> None:
                session_broadcast_tasks.discard(done_task)
                try:
                    done_task.result()
                except asyncio.CancelledError:
                    logger.debug("Session change broadcast task cancelled")
                except Exception:
                    logger.exception("Session change broadcast task failed")

            def _broadcast_session_change(event: str, session_id: str) -> None:
                if not ws_server or listener_loop.is_closed() or app.state.session_broadcast_closed:
                    return

                def _schedule() -> None:
                    if app.state.session_broadcast_closed or listener_loop.is_closed():
                        return
                    try:
                        task = listener_loop.create_task(
                            ws_server.broadcast_session_event(event, session_id)
                        )
                    except RuntimeError:
                        logger.debug("Session change broadcast skipped because loop is closed")
                        return
                    session_broadcast_tasks.add(task)
                    task.add_done_callback(_session_broadcast_done)

                try:
                    listener_loop.call_soon_threadsafe(_schedule)
                except RuntimeError:
                    logger.debug("Session change broadcast scheduling skipped; loop is closed")

            server.session_manager.register_session_change_listener(_broadcast_session_change)
            app.state.session_change_listener = _broadcast_session_change
            logger.debug("Session change listener connected to session manager")

        if (
            ws_server
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_inter_session_msg_manager")
            and app.state.hook_manager._inter_session_msg_manager
        ):
            ws_server.inter_session_msg_manager = app.state.hook_manager._inter_session_msg_manager
            logger.debug("Inter-session message manager connected to WebSocket server")

        if (
            server.services.agent_runner
            and hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_workflow_handler")
        ):
            server.services.agent_runner.workflow_handler = app.state.hook_manager._workflow_handler
            logger.debug("Workflow handler connected to AgentRunner")

        if (
            hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_session_coordinator")
            and server.services.agent_lifecycle_monitor
        ):
            server.services.agent_lifecycle_monitor.set_session_coordinator(
                app.state.hook_manager._session_coordinator
            )
            logger.debug("Session coordinator connected to agent lifecycle monitor")

        if (
            hasattr(app.state, "hook_manager")
            and hasattr(app.state.hook_manager, "_session_coordinator")
            and server.services.completion_registry
        ):
            app.state.hook_manager._session_coordinator.set_completion_registry(
                server.services.completion_registry
            )
            logger.debug("Completion registry connected to session coordinator")

        app.state.server = server
        from gobby.servers.routes.llm import start_vision_temp_cleanup_task

        start_vision_temp_cleanup_task(app)

        runtime_manager = getattr(server.services, "web_chat_runtime_manager", None)

        async def _sync_existing_codex_sessions() -> None:
            if not getattr(app.state, "codex_adapter", None):
                return
            try:
                synced = await asyncio.wait_for(
                    app.state.codex_adapter.sync_existing_sessions(),
                    timeout=_CODEX_SYNC_TIMEOUT_SECONDS,
                )
                logger.debug("Synced %s existing Codex sessions", synced)
            except TimeoutError:
                logger.warning(
                    "Timed out syncing existing Codex sessions after %.1fs",
                    _CODEX_SYNC_TIMEOUT_SECONDS,
                )
            except Exception as e:
                logger.warning("Failed to sync existing Codex sessions: %s", e)

        app.state.codex_adapter = None
        app.state.codex_sync_task = None
        codex_adapter_cls = codex_adapter_cls_getter()
        if server.codex_client and codex_adapter_cls.is_codex_available():
            codex_adapter = codex_adapter_cls(hook_manager=app.state.hook_manager)
            codex_adapter.attach_to_client(server.codex_client)
            app.state.codex_adapter = codex_adapter
            logger.debug("CodexAdapter attached to CodexAppServerClient")

            if server.codex_client.is_connected:
                app.state.codex_sync_task = asyncio.create_task(_sync_existing_codex_sessions())

        if config and config.tmux.enabled:
            try:
                from gobby.agents.tmux import set_tmux_pane_monitor
                from gobby.agents.tmux.pane_monitor import TmuxPaneMonitor

                detection_registry = server.services.detection_registry
                if detection_registry is None:
                    raise RuntimeError("Tmux pane monitor requires a detection registry")
                monitor = TmuxPaneMonitor(
                    session_end_callback=app.state.hook_manager.event_handlers.handle_session_end,
                    detection_registry=detection_registry,
                    config=config.tmux,
                    session_manager=app.state.hook_manager._session_manager,
                    attention_manager=server.services.attention_manager,
                )
                set_tmux_pane_monitor(monitor)
                await monitor.start()
                logger.debug("TmuxPaneMonitor started")
            except Exception as e:
                logger.warning("Failed to start TmuxPaneMonitor: %s", e)

        try:
            from gobby.sessions.liveness_monitor import SessionLivenessMonitor

            session_storage = app.state.hook_manager._session_manager
            liveness_monitor = SessionLivenessMonitor(
                session_storage=session_storage,
                dispatch_summaries_fn=getattr(
                    app.state.hook_manager, "_dispatch_session_summaries", None
                ),
                message_processor_resolver=lambda: server.message_processor,
                tmux_config=config.tmux if config else None,
            )
            app.state.liveness_monitor = liveness_monitor
            app.state.hook_manager.event_handlers.set_liveness_monitor(liveness_monitor)
            await liveness_monitor.start()
            logger.debug("SessionLivenessMonitor started")
        except Exception as e:
            logger.warning("Failed to start SessionLivenessMonitor: %s", e)

        if mcp_app is not None:
            async with mcp_app.router.lifespan_context(app):
                logger.debug("MCP server lifespan initialized")
                yield
            logger.debug("MCP server lifespan shutdown complete")
        else:
            yield

        logger.debug("Shutting down Gobby HTTP server")
        if hasattr(app.state, "session_change_listener") and server.session_manager is not None:
            server.session_manager.unregister_session_change_listener(
                app.state.session_change_listener
            )
            del app.state.session_change_listener
            logger.debug("Session change listener disconnected from session manager")

        if hasattr(app.state, "session_broadcast_closed"):
            app.state.session_broadcast_closed = True

        broadcast_tasks = list(getattr(app.state, "session_broadcast_tasks", ()))
        if broadcast_tasks:
            for task in broadcast_tasks:
                task.cancel()
            results = await asyncio.gather(*broadcast_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    logger.warning(
                        "Session change broadcast task failed during shutdown",
                        exc_info=(type(result), result, result.__traceback__),
                    )
            app.state.session_broadcast_tasks.clear()
            logger.debug("Session change broadcast tasks stopped")

        voice_cleanup = getattr(ws_server, "cleanup_voice", None) if ws_server else None
        if voice_cleanup:
            try:
                cleanup_result = voice_cleanup()
                if inspect.isawaitable(cleanup_result):
                    await cleanup_result
                logger.debug("Voice resources cleaned up")
            except Exception as e:
                logger.warning("Failed to clean up voice resources: %s", e)

        from gobby.servers.routes.llm import stop_vision_temp_cleanup_task

        try:
            await stop_vision_temp_cleanup_task(app)
        except Exception as e:
            logger.warning("Failed to stop vision temp cleanup task: %s", e)

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
                logger.warning("Failed to stop web chat runtime manager: %s", e)

        if hasattr(app.state, "liveness_monitor") and app.state.liveness_monitor:
            try:
                await app.state.liveness_monitor.stop()
                app.state.liveness_monitor = None
                logger.debug("SessionLivenessMonitor stopped")
            except Exception as e:
                logger.warning("Failed to stop SessionLivenessMonitor: %s", e)

        try:
            from gobby.agents.tmux import get_tmux_pane_monitor, set_tmux_pane_monitor

            pane_monitor = get_tmux_pane_monitor()
            if pane_monitor:
                await pane_monitor.stop()
                set_tmux_pane_monitor(None)
                logger.debug("TmuxPaneMonitor stopped")
        except Exception as e:
            logger.warning("Failed to stop TmuxPaneMonitor: %s", e)

        if hasattr(app.state, "pending_interaction_manager"):
            try:
                await app.state.pending_interaction_manager.cleanup()
                logger.debug("PendingInteractionManager cleanup complete")
            except Exception as e:
                logger.exception("PendingInteractionManager cleanup failed: %s", e)

        if hasattr(app.state, "hook_manager"):
            message_processor = server.message_processor
            if message_processor is not None:
                message_processor.set_hook_manager(None)
            hook_manager_shutdown = getattr(app.state.hook_manager, "shutdown_async", None)
            if not callable(hook_manager_shutdown):
                raise RuntimeError("Hook manager must provide callable shutdown_async()")
            shutdown_result = hook_manager_shutdown()
            if not inspect.isawaitable(shutdown_result):
                raise RuntimeError("Hook manager must provide callable shutdown_async()")
            await shutdown_result
            app.state.hook_manager_shutdown_complete = True
            if server._hook_manager is app.state.hook_manager:
                server._hook_manager = None
            logger.debug("HookManager shutdown complete")

        await server._process_shutdown()

        server._running = False

    return lifespan
