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
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from gobby.mcp_proxy.manager import MCPClientManager
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, ConnectionClosedError
from websockets.http11 import Response

logger = logging.getLogger(__name__)


@dataclass
class WebSocketConfig:
    """Configuration for WebSocket server."""

    host: str = "localhost"
    port: int = 8765
    ping_interval: int = 30  # seconds
    ping_timeout: int = 10  # seconds
    max_message_size: int = 2 * 1024 * 1024  # 2MB


class WebSocketServer:
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
        config = WebSocketConfig(host="0.0.0.0", port=8765)

        async with WebSocketServer(config, mcp_manager) as server:
            await server.serve_forever()
        ```
    """

    def __init__(
        self,
        config: WebSocketConfig,
        mcp_manager: MCPClientManager,
        auth_callback: Callable[[str], Coroutine[Any, Any, str | None]] | None = None,
    ):
        """
        Initialize WebSocket server.

        Args:
            config: WebSocket server configuration
            mcp_manager: MCP client manager for tool routing
            auth_callback: Optional async function that validates token and returns user_id.
                          If None, all connections are accepted (local-first mode).
        """
        self.config = config
        self.mcp_manager = mcp_manager
        self.auth_callback = auth_callback

        # Connected clients: {websocket: client_metadata}
        self.clients: dict[Any, dict[str, Any]] = {}

        # Server instance (set when started)
        self._server: Any = None
        self._serve_task: asyncio.Task | None = None

    async def __aenter__(self) -> "WebSocketServer":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.stop()

    async def _authenticate(self, websocket: Any, request: Any) -> Response | None:
        """
        Authenticate WebSocket connection via Bearer token.

        In local-first mode (no auth_callback), all connections are accepted
        with a generated local user ID.

        Args:
            websocket: WebSocket connection
            request: HTTP request with headers

        Returns:
            None to accept connection, Response to reject
        """
        # Local-first mode: accept all connections
        if self.auth_callback is None:
            websocket.user_id = f"local-{uuid4().hex[:8]}"
            return None

        # Auth callback provided - require Bearer token
        auth_header = request.headers.get("Authorization")

        if not auth_header:
            logger.warning(
                f"Connection rejected: Missing Authorization header from {websocket.remote_address}"
            )
            return Response(401, "Unauthorized: Missing Authorization header\n", Headers())

        if not auth_header.startswith("Bearer "):
            logger.warning(
                f"Connection rejected: Invalid Authorization format from {websocket.remote_address}"
            )
            return Response(401, "Unauthorized: Expected Bearer token\n", Headers())

        token = auth_header.removeprefix("Bearer ")

        try:
            user_id = await self.auth_callback(token)

            if not user_id:
                logger.warning(
                    f"Connection rejected: Invalid token from {websocket.remote_address}"
                )
                return Response(403, "Forbidden: Invalid token\n", Headers())

            # Store user_id on websocket for handler
            websocket.user_id = user_id
            return None

        except Exception as e:
            logger.error(f"Authentication error from {websocket.remote_address}: {e}")
            return Response(500, "Internal server error\n", Headers())

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
            "connected_at": datetime.now(),
            "remote_address": websocket.remote_address,
        }

        logger.debug(
            f"Client {user_id} ({client_id}) connected from {websocket.remote_address}. "
            f"Total clients: {len(self.clients)}"
        )

        try:
            # Send welcome message
            await websocket.send(
                json.dumps(
                    {
                        "type": "connection_established",
                        "client_id": client_id,
                        "user_id": user_id,
                        "latency": websocket.latency,
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
            logger.warning(f"Client {client_id} connection error: {e}")

        except ConnectionClosed:
            logger.debug(f"Client {client_id} disconnected normally")

        except Exception:
            logger.exception(f"Unexpected error for client {client_id}")

        finally:
            # Always cleanup client state
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

        if msg_type == "tool_call":
            await self._handle_tool_call(websocket, data)

        elif msg_type == "ping":
            await self._handle_ping(websocket, data)

        elif msg_type == "subscribe":
            await self._handle_subscribe(websocket, data)

        elif msg_type == "unsubscribe":
            await self._handle_unsubscribe(websocket, data)

        else:
            logger.warning(f"Unknown message type: {msg_type}")
            await self._send_error(websocket, f"Unknown message type: {msg_type}")

    async def _handle_tool_call(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle tool_call message and route to MCP server.

        Message format:
        {
            "type": "tool_call",
            "request_id": "uuid",
            "mcp": "memory",
            "tool": "add_messages",
            "args": {...}
        }

        Response format:
        {
            "type": "tool_result",
            "request_id": "uuid",
            "result": {...}
        }

        Args:
            websocket: Client WebSocket connection
            data: Parsed tool call message
        """
        request_id = data.get("request_id")
        mcp_name = data.get("mcp")
        tool_name = data.get("tool")
        args = data.get("args", {})

        if not all([request_id, mcp_name, tool_name]):
            await self._send_error(
                websocket,
                "Missing required fields: request_id, mcp, tool",
                request_id=request_id,
            )
            return

        try:
            # Route to MCP via manager
            result = await self.mcp_manager.call_tool(mcp_name, tool_name, args)

            # Send result back to client
            await websocket.send(
                json.dumps(
                    {
                        "type": "tool_result",
                        "request_id": request_id,
                        "result": result,
                    }
                )
            )

        except ValueError as e:
            # Unknown MCP server
            await self._send_error(websocket, str(e), request_id=request_id)

        except Exception as e:
            logger.exception(f"Tool call error: {mcp_name}.{tool_name}")
            await self._send_error(websocket, f"Tool call failed: {str(e)}", request_id=request_id)

    async def _handle_ping(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle manual ping message for latency measurement.

        Sends pong response with latency value.

        Args:
            websocket: Client WebSocket connection
            data: Ping message (ignored)
        """
        await websocket.send(
            json.dumps(
                {
                    "type": "pong",
                    "latency": websocket.latency,
                }
            )
        )

    async def _send_error(
        self,
        websocket: Any,
        message: str,
        request_id: str | None = None,
        code: str = "ERROR",
    ) -> None:
        """
        Send error message to client.

        Args:
            websocket: Client WebSocket connection
            message: Error message
            request_id: Optional request ID for correlation
            code: Error code (default: "ERROR")
        """
        error_msg = {
            "type": "error",
            "code": code,
            "message": message,
        }

        if request_id:
            error_msg["request_id"] = request_id

        await websocket.send(json.dumps(error_msg))

    async def _handle_subscribe(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle subscribe message to register for specific events.

        Args:
            websocket: Client WebSocket connection
            data: Subscribe message with "events" list
        """
        events = data.get("events", [])
        if not isinstance(events, list):
            await self._send_error(websocket, "events must be a list of strings")
            return

        if not hasattr(websocket, "subscriptions"):
            websocket.subscriptions = set()

        websocket.subscriptions.update(events)
        logger.debug(f"Client {websocket.user_id} subscribed to: {events}")

        await websocket.send(
            json.dumps(
                {
                    "type": "subscribe_success",
                    "events": list(websocket.subscriptions),
                }
            )
        )

    async def _handle_unsubscribe(self, websocket: Any, data: dict[str, Any]) -> None:
        """
        Handle unsubscribe message to unregister from specific events.

        Args:
            websocket: Client WebSocket connection
            data: Unsubscribe message with "events" list
        """
        events = data.get("events", [])
        if not isinstance(events, list):
            await self._send_error(websocket, "events must be a list of strings")
            return

        current_subscriptions: set[str] = getattr(websocket, "subscriptions", set())

        # If events list is empty or contains "*", unsubscribe from all
        if not events or "*" in events:
            current_subscriptions.clear()
        else:
            for event in events:
                current_subscriptions.discard(event)

        logger.debug(f"Client {websocket.user_id} unsubscribed from: {events}")

        await websocket.send(
            json.dumps(
                {
                    "type": "unsubscribe_success",
                    "events": list(current_subscriptions),
                }
            )
        )

    async def broadcast(self, message: dict[str, Any]) -> None:
        """
        Broadcast message to all connected clients.

        Filters messages based on client subscriptions:
        1. If message type is NOT 'hook_event', always send (system messages)
        2. If message type IS 'hook_event':
           - If client has NO subscriptions, send ALL events (default behavior)
           - If client HAS subscriptions, only send if event_type in subscriptions

        Args:
            message: Dictionary to serialize and send
        """
        if not self.clients:
            logger.debug("No clients connected for broadcast")
            return

        message_str = json.dumps(message)
        sent_count = 0
        failed_count = 0

        # Pre-calculate filtering criteria
        is_hook_event = message.get("type") == "hook_event"
        event_type = message.get("event_type")

        for websocket in list(self.clients.keys()):
            try:
                # Filter logic
                if is_hook_event:
                    # If subscriptions are present, we MUST match.
                    # If NO subscriptions present, we default to sending everything (backward compatibility/simple tools)
                    # Wait, strictly speaking, empty set means empty subscriptions.
                    # But for ease of use, let's say: empty set = receive all?
                    # No, explicit is better. Let's make it:
                    # - Default (no attribute): Receive All (legacy/simple)
                    # - Empty set (subscribed then unsubscribed all): Receive None
                    # - Set with items: Filter

                    # Ensure attribute exists
                    if not hasattr(websocket, "subscriptions"):
                        # Explicitly NO subscriptions set, so sending all (None means not filtering)
                        pass

                    # Refined Logic:
                    # 1. New clients have `subscriptions = None` (implicitly if not set) -> Receive All
                    # 2. subscribe() -> sets `subscriptions` to set(...) -> Filter
                    # 3. unsubscribe(*) -> sets `subscriptions` to empty set() -> Receive None

                    subs = getattr(websocket, "subscriptions", None)
                    if subs is not None:
                        # Filtering active
                        if event_type not in subs and "*" not in subs:
                            continue

                await websocket.send(message_str)
                sent_count += 1
            except ConnectionClosed:
                # Client disconnecting, will be cleaned up in handler
                failed_count += 1
            except Exception as e:
                logger.warning(f"Broadcast failed for client: {e}")
                failed_count += 1

        logger.debug(f"Broadcast complete: {sent_count} sent, {failed_count} failed")

    async def broadcast_session_update(self, event: str, **kwargs: Any) -> None:
        """
        Broadcast session update to all clients.

        Convenience method for sending session_update messages.

        Args:
            event: Event type (e.g., "token_refreshed", "logout")
            **kwargs: Additional event data
        """
        message = {
            "type": "session_update",
            "event": event,
            **kwargs,
        }

        await self.broadcast(message)

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

        logger.debug(f"WebSocket server started on ws://{self.config.host}:{self.config.port}")

    async def stop(self) -> None:
        """
        Stop WebSocket server and close all connections.

        Gracefully closes all client connections and shuts down server.
        """
        if self._server is None:
            logger.warning("WebSocket server not started")
            return

        logger.debug("Stopping WebSocket server...")

        # Close server (stops accepting new connections)
        self._server.close()
        await self._server.wait_closed()

        # Close remaining client connections
        for websocket in list(self.clients.keys()):
            try:
                await websocket.close(code=1001, reason="Server shutting down")
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
