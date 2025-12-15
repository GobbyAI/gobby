"""
DaemonClient - HTTP communication with Gobby daemon.

This module provides a clean interface for communicating with the Gobby daemon's
HTTP API. It handles health checks, authentication verification, and HTTP API calls.

The DaemonClient is session-agnostic and thread-safe, designed to be shared across
multiple sessions while maintaining cached health status for performance.

Example:
    ```python
    from gobby.utils.daemon_client import DaemonClient

    client = DaemonClient(host="localhost", port=8765)

    # Check daemon health
    is_healthy, error = client.check_health()

    # Call HTTP API endpoint
    response = client.call_http_api("/sessions/register", method="POST", json_data={
        "external_id": "abc123"
    })
    ```
"""

import logging
import threading
from typing import Any, ClassVar, cast

import httpx


class DaemonClient:
    """
    Client for communicating with Gobby daemon HTTP API.

    Provides methods for:
    - Health checking with caching
    - Authentication verification
    - HTTP API calls

    Thread-safe and session-agnostic.

    Attributes:
        url: Base URL for daemon HTTP API
        timeout: Request timeout in seconds
        logger: Logger instance for this client
    """

    # Status text mapping (class-level constant)
    DAEMON_STATUS_TEXT: ClassVar[dict[str, str]] = {
        "not_running": "Not Running",
        "cannot_access": "Cannot Access",
        "ready": "Ready",
    }

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8765,
        timeout: float = 5.0,
        logger: logging.Logger | None = None,
    ):
        """
        Initialize DaemonClient.

        Args:
            host: Daemon host address
            port: Daemon port number
            timeout: HTTP request timeout in seconds
            logger: Optional logger instance (creates one if not provided)
        """
        self.url = f"http://{host}:{port}"
        self.timeout = timeout
        self.logger = logger or logging.getLogger(__name__)

        # Health status cache (thread-safe)
        self._cache_lock = threading.Lock()
        self._cached_is_ready: bool | None = None
        self._cached_message: str | None = None
        self._cached_status: str | None = None
        self._cached_error: str | None = None

    def check_health(self) -> tuple[bool, str | None]:
        """
        Check if daemon is available and healthy.

        Returns:
            Tuple of (is_healthy, error_reason) where:
            - is_healthy: True if daemon is healthy, False otherwise
            - error_reason: None if healthy, otherwise error description
        """
        try:
            response = httpx.get(
                f"{self.url}/admin/status",
                timeout=self.timeout,
            )
            is_healthy = response.status_code == 200
            if is_healthy:
                self.logger.info(f"Daemon health check passed at {self.url}")
                return True, None
            else:
                error_reason = f"HTTP {response.status_code}"
                self.logger.warning(f"Daemon health check failed: status {response.status_code}")
                return False, error_reason
        except Exception as e:
            error_msg = str(e)
            # Check if it's a connection refused (daemon not running)
            if "refused" in error_msg.lower() or "connection" in error_msg.lower():
                self.logger.warning(f"Daemon not running: {e}")
                return False, None  # None means daemon not running
            else:
                # Other errors (timeout, DNS, etc.)
                self.logger.error(f"Daemon health check error: {e}")
                return False, error_msg

    def check_status(self) -> tuple[bool, str | None, str, str | None]:
        """
        Check daemon health status.

        Returns:
            Tuple of (is_ready, message, status, error_reason) where:
            - is_ready: True if daemon is healthy
            - message: Human-readable status message
            - status: One of: "ready", "not_running", "cannot_access"
            - error_reason: Error details if status != "ready"
        """
        is_healthy, health_error = self.check_health()

        if not is_healthy:
            if health_error is None:
                return False, "Daemon is not running", "not_running", None
            else:
                return False, f"Cannot access daemon: {health_error}", "cannot_access", health_error

        return True, "Daemon is ready", "ready", None

    def call_http_api(
        self,
        endpoint: str,
        method: str = "POST",
        json_data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """
        Call daemon HTTP API endpoint directly (for non-MCP endpoints).

        Args:
            endpoint: API endpoint path (e.g., "/sessions/register")
            method: HTTP method (default: POST)
            json_data: JSON data to send
            timeout: Request timeout (default: uses self.timeout)

        Returns:
            Response object (httpx.Response)
        """
        url = f"{self.url}{endpoint}"
        timeout_val = timeout or self.timeout

        try:
            if method.upper() == "GET":
                response = httpx.get(url, timeout=timeout_val)
            elif method.upper() == "POST":
                response = httpx.post(url, json=json_data, timeout=timeout_val)
            elif method.upper() == "PUT":
                response = httpx.put(url, json=json_data, timeout=timeout_val)
            elif method.upper() == "DELETE":
                response = httpx.delete(url, timeout=timeout_val)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            return response

        except Exception as e:
            self.logger.error(f"HTTP API call failed: {method} {endpoint} - {e}")
            raise

    def call_mcp_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """
        Call an MCP tool via the daemon's HTTP API.

        Args:
            server_name: Name of the MCP server
            tool_name: Name of the tool to call
            arguments: Tool arguments
            timeout: Request timeout

        Returns:
            Tool execution result
        """
        endpoint = f"/mcp/{server_name}/tools/{tool_name}"
        response = self.call_http_api(
            endpoint=endpoint,
            method="POST",
            json_data=arguments,
            timeout=timeout,
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def update_status_cache(self) -> None:
        """Update cached daemon status by calling check_status()."""
        with self._cache_lock:
            (
                self._cached_is_ready,
                self._cached_message,
                self._cached_status,
                self._cached_error,
            ) = self.check_status()

            self.logger.debug(
                f"Daemon status updated: {self.DAEMON_STATUS_TEXT.get(self._cached_status, 'Unknown')}"
            )

    def get_cached_status(self) -> tuple[bool | None, str | None, str | None, str | None]:
        """
        Get cached daemon status without making HTTP calls.

        Returns:
            Tuple of (is_ready, message, status, error_reason)
            Values may be None if status hasn't been checked yet.
        """
        with self._cache_lock:
            return (
                self._cached_is_ready,
                self._cached_message,
                self._cached_status,
                self._cached_error,
            )
