"""WebSocket transport connection."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client import Transport
from mcp.shared.message import SessionMessage
from mcp.types import jsonrpc_message_adapter
from pydantic import ValidationError
from websockets.asyncio.client import connect as ws_connect
from websockets.typing import Subprotocol

from gobby.mcp_proxy.transports.base import OwnerTaskTransportConnection

if TYPE_CHECKING:
    from gobby.mcp_proxy.models import MCPServerConfig

logger = logging.getLogger("gobby.mcp.client")

# The stream pair every SDK transport yields; spelled out here because the SDK
# only publishes the ``Transport`` protocol, not its stream alias.
WebSocketStreams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
]


@asynccontextmanager
async def websocket_client(
    url: str,
    headers: dict[str, str] | None,
) -> AsyncIterator[WebSocketStreams]:
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


class WebSocketTransportConnection(OwnerTaskTransportConnection):
    """WebSocket transport connection using MCP SDK."""

    _TRANSPORT_LABEL = "WebSocket"
    _OWNER_TASK_PREFIX = "ws"

    def __init__(
        self,
        config: "MCPServerConfig",
    ) -> None:
        """Initialize WebSocket transport connection."""
        super().__init__(config)

    async def _open_transport(self, stack: AsyncExitStack) -> Transport:
        """Build the socket transport; the owner task enters and exits it."""
        if self.config.url is None:
            raise RuntimeError("URL is required for WebSocket transport")
        return websocket_client(self.config.url, self.config.headers)
