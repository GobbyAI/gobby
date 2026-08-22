"""WebSocket transport connection."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import anyio
from mcp.client import Client
from mcp.client._transport import TransportStreams
from mcp.shared.message import SessionMessage
from mcp.types import jsonrpc_message_adapter
from pydantic import ValidationError
from websockets.asyncio.client import connect as ws_connect
from websockets.typing import Subprotocol

from gobby.mcp_proxy.models import ConnectionState, MCPError
from gobby.mcp_proxy.transports.base import BaseTransportConnection

if TYPE_CHECKING:
    from gobby.config.mcp import MCPServerConfig

logger = logging.getLogger("gobby.mcp.client")


@asynccontextmanager
async def websocket_client(
    url: str,
    headers: dict[str, str] | None,
) -> AsyncIterator[TransportStreams]:
    """Open MCP WebSocket streams while forwarding configured HTTP headers."""
    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](
        0
    )
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)

    async with ws_connect(
        url,
        subprotocols=[Subprotocol("mcp")],
        additional_headers=headers,
    ) as websocket:

        async def receive_messages() -> None:
            async with read_stream_writer:
                async for raw_message in websocket:
                    try:
                        message = jsonrpc_message_adapter.validate_json(raw_message)
                        await read_stream_writer.send(SessionMessage(message))
                    except ValidationError as exc:
                        await read_stream_writer.send(exc)

        async def send_messages() -> None:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    payload = session_message.message.model_dump(
                        by_alias=True,
                        mode="json",
                        exclude_none=True,
                    )
                    await websocket.send(json.dumps(payload))

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(receive_messages)
            task_group.start_soon(send_messages)
            yield read_stream, write_stream
            task_group.cancel_scope.cancel()


class WebSocketTransportConnection(BaseTransportConnection):
    """WebSocket transport connection using MCP SDK."""

    def __init__(
        self,
        config: "MCPServerConfig",
    ) -> None:
        """Initialize WebSocket transport connection."""
        super().__init__(config)

    async def _cleanup_connect_attempt(self, *, client_entered: bool) -> None:
        """Release a partially established connection after failure or cancellation.

        ``Client.__aenter__`` unwinds the transport itself when the handshake
        fails, so only a fully entered client needs an explicit exit here.
        """
        client_ctx = self._client_context
        self._session = None
        self._client_context = None
        self._state = ConnectionState.DISCONNECTED
        cancelled_error: asyncio.CancelledError | None = None

        if client_entered and client_ctx is not None:
            try:
                await asyncio.wait_for(client_ctx.__aexit__(None, None, None), timeout=2.0)
            except TimeoutError:
                logger.warning("Client cleanup timed out for %s", self.config.name)
            except asyncio.CancelledError as exc:
                logger.warning("Client cleanup cancelled for %s", self.config.name)
                cancelled_error = exc
            except Exception as cleanup_error:
                logger.warning(
                    "Error during client cleanup for %s: %s",
                    self.config.name,
                    cleanup_error,
                )

        if cancelled_error is not None:
            raise cancelled_error

    async def connect(self) -> Any:
        """Connect via WebSocket transport."""
        if self._state == ConnectionState.CONNECTED:
            return self._session

        self._state = ConnectionState.CONNECTING
        client_entered = False

        try:
            # URL is required for WebSocket transport
            if self.config.url is None:
                raise RuntimeError("URL is required for WebSocket transport")

            # Client owns the socket transport and the session, and negotiates
            # the protocol era (server/discover, then initialize).
            self._client_context = Client(websocket_client(self.config.url, self.config.headers))
            client = await self._client_context.__aenter__()
            client_entered = True
            self._session = client.session

            self._state = ConnectionState.CONNECTED
            self._consecutive_failures = 0
            logger.debug("Connected to WebSocket MCP server: %s", self.config.name)

            return self._session

        except asyncio.CancelledError:
            await self._cleanup_connect_attempt(client_entered=client_entered)
            raise
        except Exception as e:
            # Handle exceptions with empty str() (EndOfStream, ClosedResourceError)
            error_msg = str(e) if str(e) else f"{type(e).__name__}: Connection closed or timed out"
            logger.error(
                "Failed to connect to WebSocket server '%s': %s", self.config.name, error_msg
            )

            await self._cleanup_connect_attempt(client_entered=client_entered)
            self._state = ConnectionState.FAILED

            # Re-raise wrapped in MCPError (don't double-wrap)
            if isinstance(e, MCPError):
                raise
            raise MCPError(f"WebSocket connection failed: {error_msg}") from e

    async def disconnect(self) -> None:
        """Disconnect from WebSocket server."""
        if self._client_context is not None:
            try:
                await asyncio.wait_for(
                    self._client_context.__aexit__(None, None, None), timeout=2.0
                )
            except TimeoutError:
                logger.warning("Client close timed out for %s", self.config.name)
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning("Error closing client for %s: %s", self.config.name, e)
            except Exception as e:
                logger.warning("Error closing client for %s: %s", self.config.name, e)
            self._client_context = None
            self._session = None

        self._state = ConnectionState.DISCONNECTED
