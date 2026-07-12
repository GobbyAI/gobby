"""WebSocket transport connection."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from mcp import ClientSession
from mcp.client.websocket import websocket_client

from gobby.mcp_proxy.models import ConnectionState, MCPError
from gobby.mcp_proxy.transports.base import BaseTransportConnection

if TYPE_CHECKING:
    from gobby.config.mcp import MCPServerConfig

logger = logging.getLogger("gobby.mcp.client")


class WebSocketTransportConnection(BaseTransportConnection):
    """WebSocket transport connection using MCP SDK."""

    def __init__(
        self,
        config: "MCPServerConfig",
        auth_token: str | None = None,
        token_refresh_callback: Callable[[], Coroutine[Any, Any, str]] | None = None,
    ) -> None:
        """Initialize WebSocket transport connection."""
        super().__init__(config, auth_token, token_refresh_callback)
        self._session_context: ClientSession | None = None

    async def _cleanup_connect_attempt(
        self,
        *,
        session_entered: bool,
        transport_entered: bool,
    ) -> None:
        """Release partially entered contexts after failed or cancelled startup."""
        session_ctx = self._session_context
        transport_ctx = self._transport_context
        self._session = None
        self._session_context = None
        self._transport_context = None
        self._state = ConnectionState.DISCONNECTED
        cancelled_error: asyncio.CancelledError | None = None

        if session_entered and session_ctx is not None:
            try:
                await asyncio.wait_for(session_ctx.__aexit__(None, None, None), timeout=2.0)
            except TimeoutError:
                logger.warning("Session cleanup timed out for %s", self.config.name)
            except asyncio.CancelledError as exc:
                logger.warning("Session cleanup cancelled for %s", self.config.name)
                cancelled_error = exc
            except Exception as cleanup_error:
                logger.warning(
                    "Error during session cleanup for %s: %s",
                    self.config.name,
                    cleanup_error,
                )

        if transport_entered and transport_ctx is not None:
            try:
                await asyncio.wait_for(transport_ctx.__aexit__(None, None, None), timeout=2.0)
            except TimeoutError:
                logger.warning("Transport cleanup timed out for %s", self.config.name)
            except asyncio.CancelledError as exc:
                logger.warning("Transport cleanup cancelled for %s", self.config.name)
                cancelled_error = cancelled_error or exc
            except Exception as cleanup_error:
                logger.warning(
                    "Error during transport cleanup for %s: %s",
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

        # Track what was entered for cleanup
        transport_entered = False
        session_entered = False

        try:
            # URL is required for WebSocket transport
            if self.config.url is None:
                raise RuntimeError("URL is required for WebSocket transport")

            # Create WebSocket client context
            self._transport_context = websocket_client(self.config.url)

            # Enter the transport context to get streams
            read_stream, write_stream = await self._transport_context.__aenter__()
            transport_entered = True

            # Save the context manager itself so we can call __aexit__ on it later
            self._session_context = ClientSession(read_stream, write_stream)
            self._session = await self._session_context.__aenter__()
            session_entered = True

            await self._session.initialize()

            self._state = ConnectionState.CONNECTED
            self._consecutive_failures = 0
            logger.debug(f"Connected to WebSocket MCP server: {self.config.name}")

            return self._session

        except asyncio.CancelledError:
            await self._cleanup_connect_attempt(
                session_entered=session_entered,
                transport_entered=transport_entered,
            )
            raise
        except Exception as e:
            # Handle exceptions with empty str() (EndOfStream, ClosedResourceError)
            error_msg = str(e) if str(e) else f"{type(e).__name__}: Connection closed or timed out"
            logger.error(f"Failed to connect to WebSocket server '{self.config.name}': {error_msg}")

            await self._cleanup_connect_attempt(
                session_entered=session_entered,
                transport_entered=transport_entered,
            )
            self._state = ConnectionState.FAILED

            # Re-raise wrapped in MCPError (don't double-wrap)
            if isinstance(e, MCPError):
                raise
            raise MCPError(f"WebSocket connection failed: {error_msg}") from e

    async def disconnect(self) -> None:
        """Disconnect from WebSocket server."""
        # Exit session context manager (not the session object itself)
        if self._session_context is not None:
            try:
                await asyncio.wait_for(
                    self._session_context.__aexit__(None, None, None), timeout=2.0
                )
            except TimeoutError:
                logger.warning(f"Session close timed out for {self.config.name}")
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning(f"Error closing session for {self.config.name}: {e}")
            except Exception as e:
                logger.warning(f"Error closing session for {self.config.name}: {e}")
            self._session_context = None
            self._session = None

        if self._transport_context is not None:
            try:
                await asyncio.wait_for(
                    self._transport_context.__aexit__(None, None, None), timeout=2.0
                )
            except TimeoutError:
                logger.warning(f"Transport close timed out for {self.config.name}")
            except RuntimeError as e:
                # Expected when exiting cancel scope from different task
                if "cancel scope" not in str(e):
                    logger.warning(f"Error closing transport for {self.config.name}: {e}")
            except Exception as e:
                logger.warning(f"Error closing transport for {self.config.name}: {e}")
            self._transport_context = None

        self._state = ConnectionState.DISCONNECTED
