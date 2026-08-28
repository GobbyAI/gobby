"""
WebSocket server for real-time bidirectional communication.

Provides tool call proxying, session broadcasting, and connection management
with optional authentication and ping/pong keepalive.

Local-first version: Authentication is optional (defaults to always-allow).
"""

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.config._loading import bootstrap_overlaid_config
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.servers.chat_session_base import ChatSessionProtocol
from gobby.servers.websocket.auth import AuthMixin
from gobby.servers.websocket.broadcast import BroadcastMixin
from gobby.servers.websocket.chat import ChatMixin
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
from gobby.servers.websocket.handlers import HandlerMixin
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.session_control import SessionControlMixin
from gobby.servers.websocket.tmux import TmuxMixin
from gobby.servers.websocket.voice import VoiceMixin
from gobby.utils.json_helpers import json_dumps

logger = logging.getLogger(__name__)
websockets_logger = logging.getLogger("websockets.server")


if TYPE_CHECKING:
    from gobby.config.runtime import ConfigRuntime, RuntimeActiveBundle
    from gobby.hooks.broadcaster import HookEventBroadcaster
    from gobby.hooks.event_handlers import EventHandlers
    from gobby.hooks.webhooks import WebhookDispatcher
    from gobby.storage.executor import DatabaseExecutor
    from gobby.storage.inter_session_messages import InterSessionMessageManager
    from gobby.storage.sessions import SessionManager
    from gobby.workflows.hooks import WorkflowHookHandler


class WebSocketServer(
    VoiceMixin, TmuxMixin, SessionControlMixin, ChatMixin, HandlerMixin, AuthMixin, BroadcastMixin
):
    """
    WebSocket server for real-time communication.

    Provides:
    - Optional Bearer token authentication via handshake headers
    - JSON-RPC style message protocol
    - Tool call routing to MCP servers
    - Session update broadcasting
    - Automatic ping/pong keepalive
    - Connection tracking and cleanup

    Example:
        ```python
        config = WebSocketConfig(host="localhost", port=60888)

        async with WebSocketServer(config, mcp_manager) as server:
            await server.serve_forever()
        ```
    """

    def __init__(
        self,
        config: WebSocketConfig,
        mcp_manager: MCPClientManager,
        auth_callback: Callable[[str], Coroutine[Any, Any, str | None]],
        stop_registry: Any = None,
        session_manager: "SessionManager | None" = None,
        db_executor: "DatabaseExecutor | None" = None,
        daemon_config: DaemonConfig | None = None,
        bootstrap_config: BootstrapConfig | None = None,
        config_runtime: "ConfigRuntime | None" = None,
        internal_manager: Any = None,
        web_chat_session_registry: WebChatSessionRegistry | None = None,
        tool_proxy_getter: Callable[[], Any | None] | None = None,
        completion_registry: Any = None,
    ):
        """
        Initialize WebSocket server.

        Args:
            config: WebSocket server configuration
            mcp_manager: MCP client manager for tool routing
            auth_callback: Async function that validates a token and returns a user ID.
            stop_registry: Optional StopRegistry for handling stop requests from clients.
            session_manager: Optional SessionManager for persisting web-chat sessions.
            db_executor: Optional bounded executor for daemon database work.
            daemon_config: Optional DaemonConfig for voice and other features.
            internal_manager: Optional InternalRegistryManager for routing to internal MCP servers.
            web_chat_session_registry: Shared live web-chat session registry.
            tool_proxy_getter: Lazy accessor for schema-validating internal tool dispatch.
        """
        self.config = config
        self.mcp_manager = mcp_manager
        self.auth_callback = auth_callback
        self.stop_registry = stop_registry
        self.internal_manager = internal_manager
        self.tool_proxy_getter = tool_proxy_getter
        self.completion_registry = completion_registry
        self.session_manager = cast(Any, session_manager)
        self.db_executor = db_executor
        self._startup_daemon_config = daemon_config.model_copy(deep=True) if daemon_config else None
        self._bootstrap_config = bootstrap_config or BootstrapConfig()
        self._daemon_config_cache: tuple[Any, DaemonConfig] | None = None
        self._daemon_config_cache_lock = threading.Lock()
        self._runtime_bundle_context: ContextVar[tuple[bool, RuntimeActiveBundle | None]] = (
            ContextVar("gobby_websocket_runtime_bundle", default=(False, None))
        )
        self.config_runtime = config_runtime
        self.workflow_handler: WorkflowHookHandler | None = None
        self.event_handlers: EventHandlers | None = None
        self.webhook_dispatcher: WebhookDispatcher | None = None
        self.hook_broadcaster: HookEventBroadcaster | None = None
        self.inter_session_msg_manager: InterSessionMessageManager | None = None
        self.web_chat_runtime_manager: Any | None = None
        self.terminal_manager: Any | None = None
        self.terminal_runtime_registry: Any | None = None
        self.terminal_config: Any | None = None

        # Connected clients: {websocket: client_metadata}
        self.clients: dict[Any, dict[str, Any]] = {}

        self.web_chat_session_registry = (
            web_chat_session_registry if web_chat_session_registry else WebChatSessionRegistry()
        )
        self.web_chat_session_registry.bind_clear_lifecycle(
            self,
            db=getattr(session_manager, "db", None) if session_manager is not None else None,
        )

        # Persistent chat sessions keyed by conversation_id (survive disconnects)
        self._chat_sessions: dict[str, ChatSessionProtocol] = (
            self.web_chat_session_registry.sessions
        )

        # Active chat streaming tasks per conversation_id (for cancellation)
        self._active_chat_tasks: dict[str, asyncio.Task[None]] = (
            self.web_chat_session_registry.active_tasks
        )

        # Pending chat modes queued before session creation
        self._pending_modes: dict[str, str] = {}

        # Pending worktree path overrides queued before session creation
        self._pending_worktree_paths: dict[str, str] = {}

        # Pending agent name overrides queued before session creation
        self._pending_agents: dict[str, str] = {}

        # Pending project overrides queued before session creation
        self._pending_projects: dict[str, str] = {}

        # Pending provider overrides queued before session creation
        self._pending_providers: dict[str, str] = {}

        # Last update time for pending conversation configuration.
        self._pending_config_updated_at: dict[str, datetime] = {}

        # Hidden context to inject on the first post-resume user turn
        self._pending_inject_contexts: dict[str, str] = {}

        # Dispatch table for message routing (lazily populated in _handle_message)
        self._dispatch_table: dict[str, Callable[..., Coroutine[Any, Any, None]]] = {}

        # Initialize tmux subsystem
        self._init_tmux()

        # Initialize voice subsystem
        self._init_voice()

        # Server instance (set when started)
        self._server: Any = None
        self._serve_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

    def configure_terminals(
        self,
        terminal_manager: Any,
        runtime_registry: Any,
        terminal_config: Any | None = None,
    ) -> None:
        """Attach composition-root terminal services after construction."""
        self.terminal_manager = terminal_manager
        self.terminal_runtime_registry = runtime_registry
        self.terminal_config = terminal_config

    @property
    def daemon_config(self) -> DaemonConfig | None:
        """Return the shared bootstrap-overlaid projection for the active epoch."""
        runtime = self.config_runtime
        is_captured, bundle = self._runtime_bundle_context.get()
        if is_captured:
            if bundle is None:
                return self._startup_daemon_config
            snapshot = bundle.snapshot
        elif runtime is None or not getattr(runtime, "ready", False):
            return self._startup_daemon_config
        else:
            snapshot = runtime.capture().snapshot
        with self._daemon_config_cache_lock:
            cached = self._daemon_config_cache
            if cached is not None and cached[0] is snapshot:
                return cached[1]
            active = bootstrap_overlaid_config(snapshot.active, self._bootstrap_config)
            self._daemon_config_cache = (snapshot, active)
            return active

    @daemon_config.setter
    def daemon_config(self, value: Any) -> None:
        # Test seams assign a static config; clear the runtime-backed cache so
        # the assigned value only serves as the no-runtime fallback.
        with self._daemon_config_cache_lock:
            self._startup_daemon_config = value.model_copy(deep=True) if value is not None else None
            self._daemon_config_cache = None

    async def run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run daemon database work on the bounded DB executor."""
        if self.db_executor is None:
            raise RuntimeError("Database executor is not configured")
        return await self.db_executor.run(func, *args, **kwargs)

    async def __aenter__(self) -> "WebSocketServer":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.stop()

    async def _handle_connection(self, websocket: Any) -> None:
        """
        Handle WebSocket connection lifecycle.

        Registers client, processes messages, and ensures cleanup
        on disconnect. Always cleans up client state even on error.

        Args:
            websocket: Connected WebSocket client
        """
        user_id = websocket.user_id
        client_id = str(uuid4())

        # Register client
        self.clients[websocket] = {
            "id": client_id,
            "user_id": user_id,
            "connected_at": datetime.now(UTC),
            "remote_address": websocket.remote_address,
        }

        logger.debug(
            "Client %s (%s) connected from %s. Total clients: %s",
            user_id,
            client_id,
            websocket.remote_address,
            len(self.clients),
        )

        try:
            # Send welcome message with active conversation IDs
            active_conversations = list(self._chat_sessions.keys())
            await websocket.send(
                json_dumps(
                    {
                        "type": "connection_established",
                        "client_id": client_id,
                        "user_id": user_id,
                        "latency": websocket.latency,
                        "conversation_ids": active_conversations,
                    }
                )
            )

            # Re-broadcast pending interactions for active conversations
            await self._rebroadcast_pending_interactions(websocket, active_conversations)

            # Message processing loop
            async for message in websocket:
                try:
                    await self._handle_message(websocket, message)
                except ConnectionClosed:
                    raise
                except json.JSONDecodeError:
                    await self._send_error(websocket, "Invalid JSON format")
                except Exception:
                    logger.exception("Message handling error for client %s", client_id)
                    await self._send_error(websocket, "Internal server error")

        except ConnectionClosedError as e:
            logger.debug("Client %s connection closed abnormally: %s", client_id, e)

        except ConnectionClosed:
            logger.debug("Client %s disconnected normally", client_id)

        except Exception:
            logger.exception("Unexpected error for client %s", client_id)

        finally:
            # Clean up tmux bridges owned by this client
            await self._cleanup_tmux_client(websocket)
            # Always cleanup client state (but NOT chat sessions — they persist)
            metadata = self.clients.pop(websocket, None)
            attached_session_id = metadata.get("attached_session_id") if metadata else None
            if isinstance(attached_session_id, str):
                await self._cleanup_attached_tts(attached_session_id)
            logger.debug(
                "Client %s cleaned up. Remaining clients: %s", client_id, len(self.clients)
            )

    async def handle_connection(self, websocket: Any) -> None:
        """Run the public WebSocket connection lifecycle entry point."""
        await self._handle_connection(websocket)

    async def _handle_message(self, websocket: Any, message: str) -> None:
        """
        Route incoming message to appropriate handler.

        Supports message types:
        - tool_call: Route to MCP server
        - ping: Manual latency check
        - Other types: Log warning

        Args:
            websocket: Sender's WebSocket connection
            message: JSON string message
        """
        data = json.loads(message)
        if not isinstance(data, dict):
            await self._send_error(websocket, "Message must be a JSON object")
            return

        data = cast(dict[str, Any], data)
        msg_type = data.get("type")

        # Lazily initialize dispatch table
        if not self._dispatch_table:
            self._dispatch_table = {
                "tool_call": self._handle_tool_call,
                "ping": self._handle_ping,
                "subscribe": self._handle_subscribe,
                "unsubscribe": self._handle_unsubscribe,
                "stop_request": self._handle_stop_request,
                "terminal_input": self._handle_terminal_input,
                "chat_message": self._handle_chat_message,
                "stop_chat": self._handle_stop_chat,
                "ask_user_response": self._handle_ask_user_response,
                "tool_approval_response": self._handle_tool_approval_response,
                "terminal_list": self._handle_terminal_list,
                "terminal_attach": self._handle_terminal_attach,
                "terminal_detach": self._handle_terminal_detach,
                "terminal_create": self._handle_terminal_create,
                "terminal_kill": self._handle_terminal_kill,
                "terminal_resize": self._handle_terminal_resize,
                "terminal_take_control": self._handle_terminal_take_control,
                "terminal_release_control": self._handle_terminal_release_control,
                "terminal_set_viewport": self._handle_terminal_set_viewport,
                "terminal_set_scroll_offset": self._handle_terminal_set_scroll_offset,
                "terminal_paste": self._handle_terminal_paste,
                "clear_chat": self._handle_clear_chat,
                "delete_chat": self._handle_delete_chat,
                "set_mode": self._handle_set_mode,
                "plan_approval_response": self._handle_plan_approval_response,
                "set_project": self._handle_set_project,
                "set_worktree": self._handle_set_worktree,
                "set_agent": self._handle_set_agent,
                "set_provider": self._handle_set_provider,
                "continue_in_chat": self._handle_continue_in_chat,
                "attach_to_session": self._handle_attach_to_session,
                "detach_from_session": self._handle_detach_from_session,
                "send_to_cli_session": self._handle_send_to_cli_session,
                "voice_audio": self._handle_voice_audio,
                "voice_mode_toggle": self._handle_voice_mode_toggle,
                "voice_prepare": self._handle_voice_prepare,
                "tts_stop": self._handle_tts_stop,
                "heartbeat": self._handle_heartbeat,
            }

        handler = self._dispatch_table.get(cast(str, msg_type))
        if handler:
            runtime = self.config_runtime
            bundle = (
                runtime.capture()
                if runtime is not None and getattr(runtime, "ready", False)
                else None
            )
            token = self._runtime_bundle_context.set((True, bundle))
            try:
                await handler(websocket, data)
            finally:
                self._runtime_bundle_context.reset(token)
        else:
            logger.warning("Unknown message type: %s", msg_type)
            await self._send_error(websocket, f"Unknown message type: {msg_type}")

    async def start(self) -> None:
        """
        Start WebSocket server.

        Creates server instance and begins accepting connections.
        Does not block - use serve_forever() or context manager.
        """
        if self._server is not None:
            logger.warning("WebSocket server already started")
            return

        self._server = await serve(
            self.handle_connection,
            host=self.config.host,
            port=self.config.port,
            process_request=self._authenticate,
            ping_interval=self.config.ping_interval,
            ping_timeout=self.config.ping_timeout,
            max_size=self.config.max_message_size,
            compression="deflate",
            logger=websockets_logger,
        )

        # Start idle session cleanup background task
        self._cleanup_task = asyncio.create_task(self._cleanup_idle_sessions())

        logger.debug("WebSocket server started on ws://%s:%s", self.config.host, self.config.port)

    async def stop(self) -> None:
        """
        Stop WebSocket server and close all connections.

        Gracefully closes all client connections, chat sessions, and shuts down server.
        """
        if self._server is None:
            logger.warning("WebSocket server not started")
            return

        logger.debug("Stopping WebSocket server...")

        # Cancel idle cleanup task
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # Stop all tmux bridges
        await self._cleanup_tmux()

        # Stop voice subsystem
        await self.cleanup_voice()

        # Stop all chat sessions (fire SESSION_END before each)
        for conv_id, session in list(self._chat_sessions.items()):
            await self._fire_session_end(conv_id)
            await self._cancel_active_chat(conv_id)
            await session.stop()
        self._chat_sessions.clear()
        self.web_chat_session_registry.clear()
        if hasattr(self, "_session_create_locks"):
            self._session_create_locks.clear()

        # Close server (stops accepting new connections)
        self._server.close()
        await self._server.wait_closed()

        # Close remaining client connections with timeout
        for websocket in list(self.clients.keys()):
            try:
                await asyncio.wait_for(
                    websocket.close(code=1001, reason="Server shutting down"), timeout=2.0
                )
            except TimeoutError:
                logger.warning("Client connection close timed out")
            except Exception as e:
                logger.warning("Error closing client connection: %s", e)

        self._server = None
        logger.debug("WebSocket server stopped")

    async def serve_forever(self) -> None:
        """
        Run server until cancelled.

        Blocks forever until interrupted (Ctrl+C) or task cancelled.
        Use in main() for standalone server operation.
        """
        if self._server is None:
            raise RuntimeError("Server not started. Call start() first.")

        try:
            await asyncio.Future()  # Run forever
        except asyncio.CancelledError:
            logger.debug("Server cancelled, shutting down...")
            await self.stop()
            raise

    def get_client_count(self) -> int:
        """
        Get number of connected clients.

        Returns:
            Count of active client connections
        """
        return len(self.clients)

    def get_clients_info(self) -> list[dict[str, Any]]:
        """
        Get information about all connected clients.

        Returns:
            List of client metadata dictionaries
        """
        return [
            {
                "id": metadata["id"],
                "user_id": metadata["user_id"],
                "connected_at": metadata["connected_at"].isoformat(),
                "remote_address": str(metadata["remote_address"]),
            }
            for metadata in self.clients.values()
        ]
