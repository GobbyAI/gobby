"""Linear integration via official Linear MCP server.

This module provides a LinearIntegration class that delegates to the official
Linear MCP server for all Linear operations.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager

__all__ = ["LinearIntegration"]


class LinearIntegration:
    """Integration with Linear via the official Linear MCP server.

    This class provides a high-level interface for checking Linear MCP availability
    and generating graceful error messages when the server is unavailable.

    The integration delegates all actual Linear operations to the official
    Linear MCP server, avoiding the need for custom API client code.

    Attributes:
        server_name: Name of the Linear MCP server in configuration.
        mcp_manager: The MCPClientManager instance for server access.
    """

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        server_name: str = "linear",
        cache_ttl_seconds: float = 30.0,
    ) -> None:
        """Initialize LinearIntegration.

        Args:
            mcp_manager: MCPClientManager instance for accessing MCP servers.
            server_name: Name of the Linear MCP server. Defaults to "linear".
            cache_ttl_seconds: How long to cache availability checks. Defaults to 30s.
        """
        self.mcp_manager = mcp_manager
        self.server_name = server_name
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cached_available: bool | None = None
        self._cache_timestamp: float | None = None

    def is_available(self) -> bool:
        """Check if Linear MCP server is available.

        Returns True if:
        - The server is configured (has_server returns True)
        - The server is connected (health state is "connected")

        Results are cached for cache_ttl_seconds to avoid excessive checks.

        Returns:
            True if Linear MCP server is available, False otherwise.
        """
        # Check cache
        if self._cached_available is not None and self._cache_timestamp is not None:
            age = time.time() - self._cache_timestamp
            if age < self._cache_ttl_seconds:
                return self._cached_available

        # Check availability
        available = self._check_availability()

        # Update cache
        self._cached_available = available
        self._cache_timestamp = time.time()

        return available

    def _check_availability(self) -> bool:
        """Perform actual availability check without caching."""
        # Check if server is configured
        if not self.mcp_manager.has_server(self.server_name):
            return False

        # Check if server is connected. Lazy CLI managers may not have opened
        # the stdio connection yet; configured lazy servers are still usable
        # because the first tool call connects on demand.
        health = self.mcp_manager.health
        if self.server_name not in health:
            return getattr(self.mcp_manager, "lazy_connect", False) is True

        server_health = health[self.server_name]
        # Handle both object with .state attribute and dict with 'state' key
        state = getattr(server_health, "state", None)
        if state is None and isinstance(server_health, dict):
            state = server_health.get("state")

        if state == "connected":
            return True
        return state == "pending" and getattr(self.mcp_manager, "lazy_connect", False) is True

    def clear_cache(self) -> None:
        """Clear the availability cache, forcing next is_available() to check fresh."""
        self._cached_available = None
        self._cache_timestamp = None

    def get_unavailable_reason(self) -> str | None:
        """Get a human-readable reason why Linear MCP is unavailable.

        Returns:
            A string explaining why Linear is unavailable, or None if available.
        """
        if self.is_available():
            return None

        # Check if server is configured
        if not self.mcp_manager.has_server(self.server_name):
            return (
                f"Linear MCP server '{self.server_name}' is not configured. "
                "Add it to your gobby configuration or use `gobby mcp add linear`."
            )

        # Server is configured but not connected
        health = self.mcp_manager.health
        if self.server_name not in health:
            return (
                f"Linear MCP server '{self.server_name}' has no health status. "
                "The server may not have been started yet."
            )

        server_health = health[self.server_name]
        state = getattr(server_health, "state", None)
        if state is None and isinstance(server_health, dict):
            state = server_health.get("state")

        return (
            f"Linear MCP server '{self.server_name}' is not connected "
            f"(current state: {state}). "
            "Check your Linear API key and server configuration."
        )

    def require_available(self) -> None:
        """Require that Linear MCP is available, raising if not.

        Raises:
            RuntimeError: If Linear MCP server is unavailable.
        """
        if not self.is_available():
            reason = self.get_unavailable_reason()
            raise RuntimeError(f"Linear integration unavailable: {reason}")
