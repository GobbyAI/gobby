"""
WebSocket server for real-time bidirectional communication.

Provides tool call proxying, session broadcasting, and connection management
with optional authentication and ping/pong keepalive.

Local-first version: Authentication is optional (defaults to always-allow).
"""

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.auth import AuthMixin
from gobby.servers.websocket.broadcast import BroadcastMixin
from gobby.servers.websocket.chat import ChatMixin
from gobby.servers.websocket.handlers import HandlerMixin
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.session_control import SessionControlMixin
from gobby.servers.websocket.tmux import TmuxMixin
from gobby.servers.websocket.voice import VoiceMixin

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from gobby.storage.session_messages import LocalSessionMessageManager
    from gobby.storage.sessions import LocalSessionManager


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
        config = WebSocketConfig(host="0.0.0.0", port=60888)

        async with WebSocketServer(config, mcp_manager) as server:
            await server.serve_forever()
        ```
    """

    def __init__(
        self,
        config: WebSocketConfig,
        mcp_manager: MCPClientManager,
        auth_callback: Callable[[str], Coroutine[Any, Any, str | None]] | None = None,
        stop_registry: Any = None,
        session_manager: "LocalSessionManager | None" = None,
        message_manager: "LocalSessionMessageManager | None" = None,
        daemon_config: Any = None,
        internal_manager: Any = None,
    ):
        """
        Initialize WebSocket server.

        Args:
            config: WebSocket server configuration
            mcp_manager: MCP client manager for tool routing
            auth_callback: Optional async function that validates token and returns user_id.
                          If None, all connections are accepted (local-first mode).
            stop_registry: Optional StopRegistry for handling stop requests from clients.
            session_manager: Optional LocalSessionManager for persisting web-chat sessions.
            message_manager: Optional LocalSessionMessageManager for persisting chat messages.
            daemon_config: Optional DaemonConfig for voice and other features.
            internal_manager: Optional InternalRegistryManager for routing to internal MCP servers.
        """
        self.config = config
        self.mcp_manager = mcp_manager
        self.auth_callback = auth_callback
        self.stop_registry = stop_registry
        self.internal_manager = internal_manager
        self.session_manager = session_manager
        self.message_manager = message_manager
        self.daemon_config = daemon_config
        self.workflow_handler: Any = None  # WorkflowHookHandler from HookManager
        self.event_handlers: Any = None  # EventHandlers from HookManager
        self.webhook_dispatcher: Any = None  # WebhookDispatcher from HookManager
        self.hook_broadcaster: Any = None  # HookEventBroadcaster for audit trail
        self.inter_session_msg_manager: Any = None  # InterSessionMessageManager for piggyback

        # Connected clients: {websocket: client_metadata}
        self.clients: dict[Any, dict[str, Any]] = {}

        # Persistent chat sessions keyed by conversation_id (survive disconnects)
        self._chat_sessions: dict[str, ChatSession] = {}

        # Active chat streaming tasks per conversation_id (for cancellation)
        self._active_chat_tasks: dict[str, asyncio.Task[None]] = {}

        # Pending chat modes queued before session creation
        self._pending_modes: dict[str, str] = {}

        # Pending worktree path overrides queued before session creation
        self._pending_worktree_paths: dict[str, str] = {}

        # Pending agent name overrides queued before session creation
        self._pending_agents: dict[str, str] = {}

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
            f"Client {user_id} ({client_id}) connected from {websocket.remote_address}. "
            f"Total clients: {len(self.clients)}"
        )

        try:
            # Send welcome message with active conversation IDs
            active_conversations = list(self._chat_sessions.keys())
            await websocket.send(
                json.dumps(
                    {
                        "type": "connection_established",
                        "client_id": client_id,
                        "user_id": user_id,
                        "latency": websocket.latency,
                        "conversation_ids": active_conversations,
                    }
                )
            )

            # Message processing loop
            async for message in websocket:
                try:
                    await self._handle_message(websocket, message)
                except json.JSONDecodeError:
                    await self._send_error(websocket, "Invalid JSON format")
                except Exception:
                    logger.exception(f"Message handling error for client {client_id}")
                    await self._send_error(websocket, "Internal server error")

        except ConnectionClosedError as e:
            logger.debug(f"Client {client_id} connection closed abnormally: {e}")

        except ConnectionClosed:
            logger.debug(f"Client {client_id} disconnected normally")

        except Exception:
            logger.exception(f"Unexpected error for client {client_id}")

        finally:
            # Clean up tmux bridges owned by this client
            await self._cleanup_tmux_client(websocket)
            # Always cleanup client state (but NOT chat sessions — they persist)
            self.clients.pop(websocket, None)
            logger.debug(f"Client {client_id} cleaned up. Remaining clients: {len(self.clients)}")

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
                "tmux_list_sessions": self._handle_tmux_list_sessions,
                "tmux_attach": self._handle_tmux_attach,
                "tmux_detach": self._handle_tmux_detach,
                "tmux_create_session": self._handle_tmux_create_session,
                "tmux_kill_session": self._handle_tmux_kill_session,
                "tmux_resize": self._handle_tmux_resize,
                "clear_chat": self._handle_clear_chat,
                "delete_chat": self._handle_delete_chat,
                "set_mode": self._handle_set_mode,
                "plan_approval_response": self._handle_plan_approval_response,
                "set_project": self._handle_set_project,
                "set_worktree": self._handle_set_worktree,
                "set_agent": self._handle_set_agent,
                "continue_in_chat": self._handle_continue_in_chat,
                "attach_to_session": self._handle_attach_to_session,
                "detach_from_session": self._handle_detach_from_session,
                "send_to_cli_session": self._handle_send_to_cli_session,
                "voice_audio": self._handle_voice_audio,
                "voice_mode_toggle": self._handle_voice_mode_toggle,
                "canvas_interaction": self._handle_canvas_interaction,
            }

        handler = self._dispatch_table.get(msg_type)
        if handler:
            await handler(websocket, data)
        else:
            logger.warning(f"Unknown message type: {msg_type}")
            await self._send_error(websocket, f"Unknown message type: {msg_type}")

    async def _handle_canvas_interaction(self, websocket: Any, data: dict[str, Any]) -> None:
        """Handle user interaction on an A2UI canvas."""
        canvas_id = data.get("canvas_id")
        action = data.get("action")
        if not canvas_id or not action:
            return

        from gobby.mcp_proxy.tools.canvas import resolve_interaction

        await resolve_interaction(canvas_id, action)

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
            self._handle_connection,
            host=self.config.host,
            port=self.config.port,
            process_request=self._authenticate,
            ping_interval=self.config.ping_interval,
            ping_timeout=self.config.ping_timeout,
            max_size=self.config.max_message_size,
            compression="deflate",
        )

        # Start idle session cleanup background task
        self._cleanup_task = asyncio.create_task(self._cleanup_idle_sessions())

        logger.debug(f"WebSocket server started on ws://{self.config.host}:{self.config.port}")

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
        await self._cleanup_voice()

        # Stop all chat sessions (fire SESSION_END before each)
        for conv_id, session in list(self._chat_sessions.items()):
            from gobby.mcp_proxy.tools.canvas import cancel_conversation_canvases

            cancel_conversation_canvases(conv_id)

            await self._fire_session_end(conv_id)
            await self._cancel_active_chat(conv_id)
            await session.stop()
        self._chat_sessions.clear()

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
                logger.warning(f"Error closing client connection: {e}")

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
