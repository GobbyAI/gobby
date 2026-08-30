"""
MCP Proxy data models and configuration classes.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from gobby.config.url_validation import (
    HTTP_URL_SCHEMES,
    WEBSOCKET_URL_SCHEMES,
    validate_endpoint_url,
)
from gobby.mcp_proxy.transport_types import SUPPORTED_TRANSPORTS, URL_TRANSPORTS


class ConnectionState(str, Enum):
    """MCP connection state."""

    PENDING = "pending"  # Configured, lazy-loaded, never connected yet
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"
    NEEDS_CONFIGURATION = "needs_configuration"
    STALE_TEMPLATE = "stale_template"
    DISABLED = "disabled"


class MCPError(Exception):
    """Base exception for MCP client errors."""

    def __init__(
        self,
        message: str,
        code: int | None = None,
        *,
        missing_secrets: list[str] | None = None,
    ):
        """
        Initialize MCP error.

        Args:
            message: Error message
            code: JSON-RPC error code (if applicable)
            missing_secrets: Secret names that blocked a fail-closed connect
        """
        super().__init__(message)
        self.code = code
        self.missing_secrets = missing_secrets


class ToolProxyErrorCode(str, Enum):
    """Structured error codes for ToolProxyService responses.

    Used by _process_tool_proxy_result to determine HTTP status codes
    without fragile string matching.
    """

    SERVER_NOT_FOUND = "SERVER_NOT_FOUND"
    SERVER_NOT_CONFIGURED = "SERVER_NOT_CONFIGURED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_BLOCKED = "TOOL_BLOCKED"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    SESSION_REQUIRED = "SESSION_REQUIRED"


class HealthState(str, Enum):
    """Connection health state for monitoring."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class MCPConnectionHealth:
    """
    Health tracking for individual MCP connection.

    Tracks connection state, consecutive failures, and last health check
    to enable health monitoring and automatic recovery.
    """

    name: str
    state: ConnectionState
    health: HealthState = HealthState.HEALTHY
    last_health_check: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    response_time_ms: float | None = None
    project_id: str | None = None
    missing_secrets: list[str] | None = None

    def record_success(self, response_time_ms: float | None = None) -> None:
        """
        Record successful operation.

        Args:
            response_time_ms: Response time in milliseconds (optional)
        """
        self.consecutive_failures = 0
        self.last_error = None
        self.health = HealthState.HEALTHY
        self.response_time_ms = response_time_ms
        self.last_health_check = datetime.now(UTC)

    def record_failure(self, error: str) -> None:
        """
        Record failed operation and update health state.

        Args:
            error: Error message from failure
        """
        self.consecutive_failures += 1
        self.last_error = error
        self.last_health_check = datetime.now(UTC)

        # Update health state based on failure count
        if self.consecutive_failures >= 5:
            self.health = HealthState.UNHEALTHY
        elif self.consecutive_failures >= 3:
            self.health = HealthState.DEGRADED


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server with transport support."""

    # Required fields (no defaults) - must come first
    name: str
    project_id: str  # UUID string for the project this server belongs to

    # Optional fields with defaults
    enabled: bool = True

    # Transport configuration
    transport: str = "http"  # "http", "stdio", "websocket", "sse"

    # HTTP/WebSocket/SSE transport
    url: str | None = None
    headers: dict[str, str] | None = None  # Custom headers (e.g., API keys)

    # Stdio transport
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None

    # OAuth/Auth (for HTTP/WebSocket)
    requires_oauth: bool = False
    oauth_provider: str | None = None  # e.g., "google", "github"

    # Tool metadata (cached summaries)
    tools: list[dict[str, str]] | None = None  # [{"name": "tool_name", "description": "..."}]

    # Server description (what it does, when to use it)
    description: str | None = None

    # Connection timeout (seconds) for establishing connections
    connect_timeout: float = 30.0

    id: str = field(default_factory=lambda: str(uuid4()))
    template_id: str | None = None
    template: str | None = None
    runtime_hook: str | None = None
    template_values: dict[str, Any] | None = None

    def validate(self) -> None:
        """Validate configuration based on transport type."""
        if not str(self.id).strip():
            raise ValueError("id must be a non-empty string")
        if self.transport not in SUPPORTED_TRANSPORTS:
            raise ValueError(
                f"Unsupported transport: {self.transport}. Supported: {list(SUPPORTED_TRANSPORTS)}"
            )

        if self.transport in URL_TRANSPORTS:
            if not self.url:
                raise ValueError(f"{self.transport} transport requires 'url' parameter")
            allowed_schemes = (
                WEBSOCKET_URL_SCHEMES if self.transport == "websocket" else HTTP_URL_SCHEMES
            )
            self.url = validate_endpoint_url(
                self.url,
                field_name=f"{self.transport} transport URL",
                allowed_schemes=allowed_schemes,
            )
        elif self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio transport requires 'command' parameter")

        # Validate connect_timeout is positive
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be a positive number")
