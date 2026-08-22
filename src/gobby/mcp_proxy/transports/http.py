"""HTTP transport connection."""

import logging
from contextlib import AsyncExitStack

import httpx2
from mcp.client import Transport
from mcp.client.streamable_http import streamable_http_client

from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.transports.base import OwnerTaskTransportConnection

logger = logging.getLogger("gobby.mcp.client")

# MCP's recommended HTTP timeout profile: 30s for connect/write/pool, 300s read
# so long-lived SSE response streams are not cut mid-request.
MCP_HTTP_TIMEOUT_SECONDS = 30.0
MCP_HTTP_READ_TIMEOUT_SECONDS = 300.0


def build_mcp_http_client(headers: dict[str, str] | None) -> httpx2.AsyncClient:
    """Build the httpx2 client handed to the Streamable HTTP transport."""
    return httpx2.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=httpx2.Timeout(MCP_HTTP_TIMEOUT_SECONDS, read=MCP_HTTP_READ_TIMEOUT_SECONDS),
    )


class HTTPTransportConnection(OwnerTaskTransportConnection):
    """Streamable HTTP transport connection using the MCP SDK."""

    _TRANSPORT_LABEL = "HTTP"
    _OWNER_TASK_PREFIX = "http"

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)

    async def _open_transport(self, stack: AsyncExitStack) -> Transport:
        """Build the Streamable HTTP transport over a client owned by ``stack``."""
        if self.config.url is None:
            raise ValueError(f"URL is required for HTTP server '{self.config.name}'")
        managed_client = await stack.enter_async_context(build_mcp_http_client(self.config.headers))
        return streamable_http_client(self.config.url, http_client=managed_client)
