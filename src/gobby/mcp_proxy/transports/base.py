"""Base transport connection abstract class."""

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from mcp import ClientSession

from gobby.mcp_proxy.models import ConnectionState, MCPServerConfig


class BaseTransportConnection:
    """
    Base class for MCP transport connections.

    All transport implementations must provide:
    - connect() -> ClientSession
    - disconnect()
    - is_connected property
    - state property
    """

    def __init__(
        self,
        config: MCPServerConfig,
        auth_token: str | None = None,
        token_refresh_callback: Callable[[], Coroutine[Any, Any, str]] | None = None,
    ):
        """
        Initialize transport connection.

        Args:
            config: Server configuration
            auth_token: Optional auth token
            token_refresh_callback: Optional callback for token refresh
        """
        self.config = config
        self._auth_token = auth_token
        self._token_refresh_callback = token_refresh_callback
        self._session: Any | None = None  # ClientSession
        self._transport_context: Any | None = None  # Transport-specific context manager
        self._state = ConnectionState.DISCONNECTED
        self._last_health_check: datetime | None = None
        self._consecutive_failures = 0

    async def connect(self) -> Any:
        """Connect and return ClientSession. Must be implemented by subclasses."""
        raise NotImplementedError

    async def disconnect(self) -> None:
        """Disconnect from server. Must be implemented by subclasses."""
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        """Check if connection is active."""
        return self._state == ConnectionState.CONNECTED and self._session is not None

    @property
    def state(self) -> ConnectionState:
        """Get current connection state."""
        return self._state

    @property
    def session(self) -> ClientSession | None:
        """Get the current client session, if connected."""
        return self._session

    def set_auth_token(self, token: str) -> None:
        """Update authentication token."""
        self._auth_token = token

    async def health_check(self, timeout: float = 5.0) -> bool:
        """
        Check connection health.

        Args:
            timeout: Health check timeout in seconds

        Returns:
            True if healthy, False otherwise
        """
        if not self.is_connected or not self._session:
            return False

        try:
            # Use asyncio.wait_for for timeout
            await asyncio.wait_for(self._session.list_tools(), timeout)
            self._last_health_check = datetime.now(UTC)
            self._consecutive_failures = 0
            return True
        except (TimeoutError, Exception):
            self._consecutive_failures += 1
            return False
