"""
DaemonClient - HTTP communication with Gobby daemon.

This module provides a clean interface for communicating with the Gobby daemon's
HTTP API. It handles health checks, authentication verification, and HTTP API calls.

The DaemonClient is session-agnostic and thread-safe, designed to be shared across
multiple sessions while maintaining cached health status for performance.

Example:
    ```python
    from gobby.utils.daemon_client import DaemonClient

    client = DaemonClient(host="localhost", port=60887)

    # Check daemon health
    is_healthy, error = client.check_health()

    # Call HTTP API endpoint
    response = client.call_http_api("/api/sessions/register", method="POST", json_data={
        "external_id": "abc123"
    })
    ```
"""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar, cast

import httpx

from gobby.files_home_http import (
    CONNECT_TIMEOUT_SECONDS,
    DEADLINE_TIMEOUT_SECONDS,
    FILES_PROXY_HOP_HEADER,
    INACTIVITY_TIMEOUT_SECONDS,
)
from gobby.shutdown_intent import ShutdownIntent, read_active_shutdown_intent
from gobby.utils.daemon_url import daemon_url, normalize_dial_host, validate_daemon_url
from gobby.utils.local_token import daemon_auth_headers

PLANNED_RESTART_MARKER_MAX_AGE_SECONDS = 120.0
DAEMON_AUTH_REMEDIATION = (
    "token missing or stale; run 'gobby install' or 'gobby auth token --rotate' on the hub "
    "machine and copy ~/.gobby/local_cli_token here"
)


class DaemonAuthenticationError(RuntimeError):
    """Raised when the daemon rejects the install-scoped bearer token."""


class DaemonClientError(RuntimeError):
    """Typed failure from a daemon HTTP request."""


class DaemonTimeoutError(DaemonClientError):
    """Connect, inactivity, or overall deadline timeout."""


class DaemonStatusError(DaemonClientError):
    """Upstream status was outside the caller-accepted set."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class DaemonHealthError(StrEnum):
    """Typed daemon health failures that callers need to classify."""

    NOT_RUNNING = "Daemon is not running"


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
        port: int | None = None,
        timeout: float = 5.0,
        logger: logging.Logger | None = None,
        *,
        url: str | None = None,
    ) -> None:
        """
        Initialize DaemonClient.

        Args:
            host: Daemon host address
            port: Daemon port number. Defaults to the resolved daemon configuration.
            timeout: HTTP request timeout in seconds
            logger: Optional logger instance (creates one if not provided)
            url: Fully resolved daemon base URL. Overrides host/port when provided.
        """
        if url is not None:
            self.url = validate_daemon_url(url, source="daemon client URL")
        elif port is None:
            self.url = daemon_url()
        else:
            self.url = f"http://{normalize_dial_host(host)}:{port}"
        self.timeout = timeout
        self.logger = logger or logging.getLogger(__name__)
        self._auth_headers = daemon_auth_headers()

        self._health_log_lock = threading.Lock()
        self._health_failed_since_last_success = False
        self._consecutive_health_timeouts = 0

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        timeout: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> "DaemonClient":
        """Create a daemon client from a resolved base URL."""
        return cls(timeout=timeout, logger=logger, url=url)

    def check_health(self) -> tuple[bool, str | None]:
        """
        Check if daemon is available and healthy.

        Returns:
            Tuple of (is_healthy, error_reason) where:
            - is_healthy: True if daemon is healthy, False otherwise
            - error_reason: None if healthy, otherwise a typed failure or error description
        """
        try:
            response = httpx.get(
                f"{self.url}/api/admin/health",
                headers=self._auth_headers,
                timeout=self.timeout,
                trust_env=False,
            )
            if response.status_code == 401:
                self._mark_health_failed()
                self.logger.warning("Daemon authentication failed: %s", DAEMON_AUTH_REMEDIATION)
                return False, DAEMON_AUTH_REMEDIATION
            is_healthy = response.status_code == 200
            if is_healthy:
                self._log_health_success()
                return True, None
            else:
                error_reason = f"HTTP {response.status_code}"
                self._mark_health_failed()
                self.logger.warning("Daemon health check failed: status %s", response.status_code)
                return False, error_reason
        except httpx.ConnectError as e:
            self._mark_health_failed()
            restart_source = self._planned_restart_source()
            if restart_source:
                self.logger.debug(
                    "Daemon not running during planned restart (%s): %s", restart_source, e
                )
            else:
                self.logger.debug("Daemon not running: %s", e)
            return False, DaemonHealthError.NOT_RUNNING
        except httpx.TimeoutException as e:
            self._record_health_timeout(e)
            return False, str(e)
        except httpx.HTTPError as e:
            self._mark_health_failed()
            self.logger.warning("Daemon health check error: %s", e)
            return False, str(e)

    def _log_health_success(self) -> None:
        with self._health_log_lock:
            was_recovering = self._health_failed_since_last_success
            log_extra = {
                "url": self.url,
                "health_failed_since_last_success": was_recovering,
            }
            self._consecutive_health_timeouts = 0
            if was_recovering:
                self._health_failed_since_last_success = False
                self.logger.info("Daemon health recovered", extra=log_extra)
            else:
                self.logger.debug("Daemon health check passed", extra=log_extra)

    def _mark_health_failed(self) -> None:
        with self._health_log_lock:
            self._health_failed_since_last_success = True
            self._consecutive_health_timeouts = 0

    def _record_health_timeout(self, error: httpx.TimeoutException) -> None:
        with self._health_log_lock:
            self._health_failed_since_last_success = True
            self._consecutive_health_timeouts += 1
            attempt = self._consecutive_health_timeouts

        if attempt == 2:
            self.logger.warning(
                "Daemon health check timed out twice consecutively",
                extra={
                    "daemon_url": self.url,
                    "timeout_streak": attempt,
                    "error": str(error),
                },
            )
        else:
            self.logger.debug(
                "Daemon health check timed out",
                extra={
                    "daemon_url": self.url,
                    "timeout_streak": attempt,
                    "error": str(error),
                },
            )

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
            if health_error is DaemonHealthError.NOT_RUNNING:
                return False, str(health_error), "not_running", str(health_error)
            else:
                return False, f"Cannot access daemon: {health_error}", "cannot_access", health_error

        return True, "Daemon is ready", "ready", None

    def _planned_restart_source(self) -> str | None:
        record = read_active_shutdown_intent(max_age_seconds=PLANNED_RESTART_MARKER_MAX_AGE_SECONDS)
        if record is None or record.stale or record.error:
            return None
        if record.intent is not ShutdownIntent.RESTART:
            return None
        return record.source

    def _merged_headers(self, headers: Mapping[str, str] | None = None) -> dict[str, str]:
        merged = dict(self._auth_headers)
        if headers:
            merged.update(headers)
        return merged

    def _join_url(self, endpoint: str) -> str:
        return f"{self.url}{endpoint}"

    def call_http_api(
        self,
        endpoint: str,
        method: str = "POST",
        json_data: dict[str, Any] | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """
        Call daemon HTTP API endpoint directly (for non-MCP endpoints).

        Args:
            endpoint: API endpoint path (e.g., "/api/sessions/register")
            method: HTTP method (default: POST)
            json_data: JSON data to send
            timeout: Request timeout (default: uses self.timeout)
            headers: Extra headers merged over the install-scoped auth headers

        Returns:
            Response object (httpx.Response)
        """
        url = self._join_url(endpoint)
        timeout_val = self.timeout if timeout is None else timeout
        request_headers = self._merged_headers(headers)

        try:
            if method.upper() == "GET":
                if json_data is None:
                    response = httpx.get(url, headers=request_headers, timeout=timeout_val)
                else:
                    response = httpx.request(
                        "GET",
                        url,
                        json=json_data,
                        headers=request_headers,
                        timeout=timeout_val,
                    )
            elif method.upper() == "POST":
                response = httpx.post(
                    url, json=json_data, headers=request_headers, timeout=timeout_val
                )
            elif method.upper() == "PUT":
                response = httpx.put(
                    url, json=json_data, headers=request_headers, timeout=timeout_val
                )
            elif method.upper() == "PATCH":
                response = httpx.patch(
                    url, json=json_data, headers=request_headers, timeout=timeout_val
                )
            elif method.upper() == "DELETE":
                if json_data is None:
                    response = httpx.delete(url, headers=request_headers, timeout=timeout_val)
                else:
                    response = httpx.request(
                        "DELETE",
                        url,
                        json=json_data,
                        headers=request_headers,
                        timeout=timeout_val,
                    )
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            if response.status_code == 401:
                raise DaemonAuthenticationError(
                    f"Daemon authentication failed: {DAEMON_AUTH_REMEDIATION}"
                )
            return response

        except Exception as e:
            self.logger.error("HTTP API call failed: %s %s - %s", method, endpoint, e)
            raise

    def _request_timeout(
        self,
        *,
        connect_timeout: float,
        inactivity_timeout: float,
    ) -> httpx.Timeout:
        return httpx.Timeout(
            connect=connect_timeout,
            pool=connect_timeout,
            read=inactivity_timeout,
            write=inactivity_timeout,
        )

    def _raise_for_status(self, response: httpx.Response, accept_statuses: tuple[int, ...]) -> None:
        if response.status_code == 401:
            raise DaemonAuthenticationError(
                f"Daemon authentication failed: {DAEMON_AUTH_REMEDIATION}"
            )
        if response.status_code not in accept_statuses:
            raise DaemonStatusError(
                response.status_code,
                f"Daemon returned HTTP {response.status_code} for {response.request.url}",
            )

    async def request_raw(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | Any = None,
        json_data: Any = None,
        hop: bool = False,
        accept_statuses: tuple[int, ...] = (200,),
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        inactivity_timeout: float = INACTIVITY_TIMEOUT_SECONDS,
        deadline_seconds: float = DEADLINE_TIMEOUT_SECONDS,
    ) -> httpx.Response:
        """Async raw request sharing URL join, auth, and error types with call_http_api."""
        request_headers = self._merged_headers(headers)
        if hop:
            request_headers[FILES_PROXY_HOP_HEADER] = "1"
        timeout = self._request_timeout(
            connect_timeout=connect_timeout,
            inactivity_timeout=inactivity_timeout,
        )
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        try:
            async with asyncio.timeout(deadline_seconds):
                response = await client.request(
                    method,
                    self._join_url(path),
                    headers=request_headers,
                    params=params,
                    content=content,
                    json=json_data,
                )
            self._raise_for_status(response, accept_statuses)
            return response
        except TimeoutError as exc:
            raise DaemonTimeoutError(f"Daemon request timed out: {method} {path}") from exc
        except httpx.TimeoutException as exc:
            raise DaemonTimeoutError(f"Daemon request timed out: {method} {path}") from exc
        finally:
            await client.aclose()

    @asynccontextmanager
    async def stream_request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        content: Any = None,
        hop: bool = False,
        accept_statuses: tuple[int, ...] = (200, 206, 304),
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        inactivity_timeout: float = INACTIVITY_TIMEOUT_SECONDS,
        deadline_seconds: float = DEADLINE_TIMEOUT_SECONDS,
    ) -> AsyncIterator[httpx.Response]:
        """Streaming request with connect, inactivity, and overall deadlines."""
        request_headers = self._merged_headers(headers)
        if hop:
            request_headers[FILES_PROXY_HOP_HEADER] = "1"
        timeout = self._request_timeout(
            connect_timeout=connect_timeout,
            inactivity_timeout=inactivity_timeout,
        )
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        try:
            async with asyncio.timeout(deadline_seconds):
                async with client.stream(
                    method,
                    self._join_url(path),
                    headers=request_headers,
                    params=params,
                    content=content,
                ) as response:
                    self._raise_for_status(response, accept_statuses)
                    yield response
        except TimeoutError as exc:
            raise DaemonTimeoutError(f"Daemon stream timed out: {method} {path}") from exc
        except httpx.TimeoutException as exc:
            raise DaemonTimeoutError(f"Daemon stream timed out: {method} {path}") from exc
        finally:
            await client.aclose()

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
        endpoint = f"/api/mcp/{server_name}/tools/{tool_name}"
        response = self.call_http_api(
            endpoint=endpoint,
            method="POST",
            json_data=arguments,
            timeout=timeout,
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())
