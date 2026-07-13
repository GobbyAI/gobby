"""Server-Sent Events transport connection."""

from contextlib import AsyncExitStack
from typing import Any

from mcp.client.sse import sse_client

from gobby.mcp_proxy.transports.http import HTTPTransportConnection


class SSETransportConnection(HTTPTransportConnection):
    """Legacy SSE transport using the MCP SDK SSE client."""

    _TRANSPORT_LABEL = "SSE"
    _OWNER_TASK_PREFIX = "sse"

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """Enter the SDK SSE client and return its read/write streams."""
        if self.config.url is None:
            raise ValueError(f"URL is required for SSE server '{self.config.name}'")
        return await stack.enter_async_context(
            sse_client(
                self.config.url,
                headers=self.config.headers,
                timeout=self.config.connect_timeout,
            )
        )
