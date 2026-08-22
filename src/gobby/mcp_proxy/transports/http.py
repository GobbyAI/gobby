"""HTTP transport connection."""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp.client import Client, Transport
from mcp.client.streamable_http import streamable_http_client

from gobby.mcp_proxy.models import ConnectionState, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.base import BaseTransportConnection

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


class HTTPTransportConnection(BaseTransportConnection):
    """HTTP/Streamable HTTP transport connection using MCP SDK.

    Uses a dedicated background task to own the streamable_http_client lifecycle,
    ensuring that context entry and exit happen in the same task (required by anyio).
    """

    _OWNER_TASK_SHUTDOWN_TIMEOUT = 2.0
    _TRANSPORT_LABEL = "HTTP"
    _OWNER_TASK_PREFIX = "http"

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._owner_task: asyncio.Task[None] | None = None
        self._disconnect_event: asyncio.Event | None = None
        self._session_ready: asyncio.Event | None = None
        self._connection_error: Exception | None = None
        self._client_context: Client | None = None

    async def connect(self) -> Any:
        """Connect via HTTP transport using a dedicated owner task."""
        if self._state == ConnectionState.CONNECTED and self._session is not None:
            return self._session

        # Clean up old connection if reconnecting
        if self._owner_task is not None:
            await self.disconnect()

        self._state = ConnectionState.CONNECTING
        self._connection_error = None

        # Create synchronization events
        self._disconnect_event = asyncio.Event()
        self._session_ready = asyncio.Event()

        # Start owner task that manages the connection lifecycle
        self._owner_task = asyncio.create_task(
            self._run_connection(), name=f"{self._OWNER_TASK_PREFIX}-conn-{self.config.name}"
        )

        # Wait for connection to be ready or fail
        timeout = self.config.connect_timeout
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=timeout)
        except TimeoutError as e:
            self._disconnect_event.set()
            await self._cleanup_owner_task()
            self._state = ConnectionState.FAILED
            raise MCPError(f"Connection timeout for {self.config.name} after {timeout}s") from e

        if self._connection_error is not None:
            error = self._connection_error
            self._connection_error = None
            await self._cleanup_owner_task()
            self._state = ConnectionState.FAILED
            raise error

        return self._session

    async def _open_transport(self, stack: AsyncExitStack) -> Transport:
        """Build the Streamable HTTP transport over a client owned by ``stack``."""
        if self.config.url is None:
            raise ValueError(f"URL is required for HTTP server '{self.config.name}'")
        managed_client = await stack.enter_async_context(build_mcp_http_client(self.config.headers))
        return streamable_http_client(self.config.url, http_client=managed_client)

    async def _run_connection(self) -> None:
        """Background task that owns the transport and session lifecycle."""
        if self._disconnect_event is None or self._session_ready is None:
            raise RuntimeError("Connection events not initialized")

        try:
            if not self.config.url:
                raise ValueError(f"URL is required for {self._TRANSPORT_LABEL} transport")

            async with AsyncExitStack() as stack:
                transport = await self._open_transport(stack)
                # Client negotiates the protocol era (server/discover with an
                # initialize fallback) before publishing the session.
                self._client_context = Client(transport)
                async with self._client_context as client:
                    self._session = client.session

                    self._state = ConnectionState.CONNECTED
                    self._consecutive_failures = 0
                    logger.debug(
                        "Connected to %s MCP server: %s",
                        self._TRANSPORT_LABEL,
                        self.config.name,
                    )

                    # Signal that connection is ready
                    self._session_ready.set()

                    # Wait until disconnect is requested
                    await self._disconnect_event.wait()

                    logger.debug("Disconnect requested for %s", self.config.name)

        except Exception as e:
            error_msg = str(e) if str(e) else f"{type(e).__name__}: Connection closed or timed out"
            logger.error(
                "Failed to connect to %s server '%s': %s",
                self._TRANSPORT_LABEL,
                self.config.name,
                error_msg,
            )

            if isinstance(e, MCPError):
                self._connection_error = e
            else:
                self._connection_error = MCPError(
                    f"{self._TRANSPORT_LABEL} connection failed: {error_msg}"
                )

            self._session_ready.set()  # Unblock waiter with error

        finally:
            self._session = None
            self._client_context = None
            self._state = ConnectionState.DISCONNECTED

    async def _cleanup_owner_task(self) -> None:
        """Clean up the owner task."""
        if self._owner_task is not None:
            if not self._owner_task.done():
                done, _pending = await asyncio.wait(
                    {self._owner_task},
                    timeout=self._OWNER_TASK_SHUTDOWN_TIMEOUT,
                )
                if done:
                    try:
                        await self._owner_task
                    except asyncio.CancelledError:
                        logger.debug("Owner task cancelled for %s", self.config.name)
                    except Exception as e:
                        logger.warning("Owner task cleanup failed for %s: %s", self.config.name, e)
                else:
                    self._owner_task.cancel()
                    try:
                        await asyncio.wait_for(
                            self._owner_task,
                            timeout=self._OWNER_TASK_SHUTDOWN_TIMEOUT,
                        )
                    except asyncio.CancelledError:
                        logger.debug("Owner task cancelled for %s", self.config.name)
                    except TimeoutError:
                        logger.warning("Owner task cleanup timed out for %s", self.config.name)
                    except Exception as e:
                        logger.warning("Owner task cleanup failed for %s: %s", self.config.name, e)
            self._owner_task = None
        self._disconnect_event = None
        self._session_ready = None

    async def disconnect(self) -> None:
        """Disconnect from HTTP server by signaling the owner task."""
        if self._disconnect_event is not None:
            self._disconnect_event.set()

        await self._cleanup_owner_task()
        self._state = ConnectionState.DISCONNECTED
