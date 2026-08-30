"""GitHub integration via official GitHub MCP server.

This module provides a GitHubIntegration class that delegates to the official
GitHub MCP server (@modelcontextprotocol/server-github) for all GitHub operations.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager

__all__ = ["GitHubIntegration"]


class GitHubIntegration:
    """Integration with GitHub via the official GitHub MCP server.

    This class provides a high-level interface for checking GitHub MCP availability
    and generating graceful error messages when the server is unavailable.

    The integration delegates all actual GitHub operations to the official
    GitHub MCP server, avoiding the need for custom API client code.

    Attributes:
        server_name: Name of the GitHub MCP server in configuration.
        mcp_manager: The MCPClientManager instance for server access.
    """

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        server_name: str = "github",
        cache_ttl_seconds: float = 30.0,
        *,
        project_id: str | None = None,
    ) -> None:
        """Initialize GitHubIntegration.

        Args:
            mcp_manager: MCPClientManager instance for accessing MCP servers.
            server_name: Name of the GitHub MCP server. Defaults to "github".
            cache_ttl_seconds: How long to cache availability checks. Defaults to 30s.
            project_id: Project scope used to resolve the github instance.
        """
        self.mcp_manager = mcp_manager
        self.server_name = server_name
        self.project_id = project_id
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cached_available: bool | None = None
        self._cache_timestamp: float | None = None

    def _resolved_config(self) -> Any:
        from gobby.mcp_proxy.services.server_resolution import as_project_id, resolve_server

        project_id = as_project_id(
            self.project_id,
            default=as_project_id(getattr(self.mcp_manager, "project_id", None)),
        )
        return resolve_server(self.mcp_manager, self.server_name, project_id=project_id)

    def is_available(self) -> bool:
        """Check if GitHub MCP server is available.

        Returns True if:
        - The server is configured (has_server returns True)
        - The server is connected (health state is "connected")

        Results are cached for cache_ttl_seconds to avoid excessive checks.

        Returns:
            True if GitHub MCP server is available, False otherwise.
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
        config = self._resolved_config()
        if config is None or not self.mcp_manager.has_server(config.id):
            return False

        health = self.mcp_manager.health
        if config.id not in health:
            return getattr(self.mcp_manager, "lazy_connect", False) is True

        server_health = health[config.id]
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
        """Get a human-readable reason why GitHub MCP is unavailable.

        Returns:
            A string explaining why GitHub is unavailable, or None if available.
        """
        if self.is_available():
            return None

        # Check if server is configured
        config = self._resolved_config()
        if config is None or not self.mcp_manager.has_server(config.id):
            return (
                f"GitHub MCP server '{self.server_name}' is not configured. "
                "Add it to your gobby configuration or use `gobby mcp add github`."
            )

        health = self.mcp_manager.health
        if config.id not in health:
            return (
                f"GitHub MCP server '{self.server_name}' has no health status. "
                "The server may not have been started yet."
            )

        server_health = health[config.id]
        state = getattr(server_health, "state", None)
        if state is None and isinstance(server_health, dict):
            state = server_health.get("state")

        return (
            f"GitHub MCP server '{self.server_name}' is not connected "
            f"(current state: {state}). "
            "Check your GitHub token and server configuration."
        )

    def require_available(self) -> None:
        """Require that GitHub MCP is available, raising if not.

        Raises:
            RuntimeError: If GitHub MCP server is unavailable.
        """
        if not self.is_available():
            reason = self.get_unavailable_reason()
            raise RuntimeError(f"GitHub integration unavailable: {reason}")
