"""Server-Sent Events transport connection."""

from contextlib import AsyncExitStack

from mcp.client import Transport
from mcp.client.sse import sse_client

from gobby.mcp_proxy.transports.http import HTTPTransportConnection


class SSETransportConnection(HTTPTransportConnection):
    """Legacy SSE transport using the MCP SDK SSE client."""

    _TRANSPORT_LABEL = "SSE"
    _OWNER_TASK_PREFIX = "sse"

    async def _open_transport(self, stack: AsyncExitStack) -> Transport:
        """Build the SDK SSE transport; it owns its own HTTP client."""
        if self.config.url is None:
            raise ValueError(f"URL is required for SSE server '{self.config.name}'")
        return sse_client(
            self.config.url,
            headers=self.config.headers,
            timeout=self.config.connect_timeout,
        )
