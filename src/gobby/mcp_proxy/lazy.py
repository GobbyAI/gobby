"""
Lazy server initialization with circuit breaker pattern.

Provides deferred MCP server connections to reduce startup time and resource usage.
Servers are connected on-demand when first accessed, with automatic retry and
circuit breaker protection against cascading failures.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger("gobby.mcp.lazy")


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation, requests pass through
    OPEN = "open"  # Circuit tripped, fail fast
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreaker:
    """
    Circuit breaker for connection protection.

    Prevents cascading failures by failing fast when a service is down.

    States:
    - CLOSED: Normal operation, all requests pass through
    - OPEN: Service is down, fail immediately without trying
    - HALF_OPEN: Service may have recovered, allow one test request
    """

    failure_threshold: int = 3  # Failures before opening circuit
    recovery_timeout: float = 30.0  # Seconds before trying half-open
    half_open_max_calls: int = 1  # Calls allowed in half-open state

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float | None = None
    half_open_calls: int = 0

    def record_success(self) -> None:
        """Record successful operation."""
        self.failure_count = 0
        self.half_open_calls = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Record failed operation and potentially trip circuit."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            # Failed during recovery test, reopen circuit
            self.state = CircuitState.OPEN
            self.half_open_calls = 0
            logger.warning("Circuit breaker reopened after half-open failure")
        elif self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning("Circuit breaker opened after %s failures", self.failure_count)

    def can_execute(self) -> bool:
        """Check if request can proceed."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if self.last_failure_time is None:
                return True

            elapsed = time.time() - self.last_failure_time
            if elapsed >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                logger.info("Circuit breaker entering half-open state")
                return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            if self.half_open_calls < self.half_open_max_calls:
                self.half_open_calls += 1
                return True
            return False

        return False


@dataclass
class RetryConfig:
    """Configuration for exponential backoff retry."""

    max_retries: int = 3
    initial_delay: float = 1.0  # seconds
    max_delay: float = 16.0  # seconds
    multiplier: float = 2.0

    def get_delay(self, attempt: int) -> float:
        """Get delay for given attempt number (0-indexed)."""
        delay = self.initial_delay * (self.multiplier**attempt)
        return min(delay, self.max_delay)


@dataclass
class LazyConnectionState:
    """
    State tracking for a lazy-connected server.

    Tracks whether a server has been connected, its circuit breaker state,
    and connection timing information.
    """

    configured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    connected_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    connection_attempts: int = 0
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    @property
    def is_connected(self) -> bool:
        """Check if server has been successfully connected."""
        return self.connected_at is not None

    def record_connection_attempt(self) -> None:
        """Record that a connection attempt is starting."""
        self.last_attempt_at = datetime.now(UTC)
        self.connection_attempts += 1

    def record_connection_success(self) -> None:
        """Record successful connection."""
        self.connected_at = datetime.now(UTC)
        self.last_error = None
        self.circuit_breaker.record_success()

    def record_connection_failure(self, error: str) -> None:
        """Record failed connection."""
        self.last_error = error
        self.circuit_breaker.record_failure()


class LazyServerConnector:
    """
    Manages lazy initialization of MCP server connections.

    Instead of connecting all servers at startup, connections are deferred
    until the first tool call or explicit request. This reduces startup time
    and avoids consuming resources for unused servers.

    Features:
    - Deferred connection on first use
    - Exponential backoff retry on connection failure
    - Circuit breaker to prevent cascading failures
    - Connection state tracking and reporting
    """

    def __init__(
        self,
        retry_config: RetryConfig | None = None,
        circuit_breaker_config: dict[str, Any] | None = None,
    ):
        """
        Initialize lazy connector.

        Args:
            retry_config: Retry configuration for connection attempts
            circuit_breaker_config: Circuit breaker settings (failure_threshold,
                                    recovery_timeout, half_open_max_calls)
        """
        self.retry_config = retry_config or RetryConfig()
        self._circuit_breaker_config = circuit_breaker_config or {}
        self._states: dict[str, LazyConnectionState] = {}
        self._connection_locks: dict[str, asyncio.Lock] = {}

    def register_server(self, server_name: str) -> None:
        """
        Register a server for lazy connection.

        Called when a server is configured but not yet connected.

        Args:
            server_name: Name of the server to register
        """
        if server_name not in self._states:
            self._states[server_name] = LazyConnectionState(
                circuit_breaker=CircuitBreaker(**self._circuit_breaker_config)
            )
            self._connection_locks[server_name] = asyncio.Lock()
            logger.debug("Registered server '%s' for lazy connection", server_name)

    def unregister_server(self, server_name: str) -> None:
        """
        Remove a server from lazy connection tracking.

        Args:
            server_name: Name of the server to unregister
        """
        self._states.pop(server_name, None)
        self._connection_locks.pop(server_name, None)

    def get_state(self, server_name: str) -> LazyConnectionState | None:
        """
        Get connection state for a server.

        Args:
            server_name: Name of the server

        Returns:
            LazyConnectionState or None if not registered
        """
        return self._states.get(server_name)

    def is_connected(self, server_name: str) -> bool:
        """
        Check if a server is connected.

        Args:
            server_name: Name of the server

        Returns:
            True if connected, False otherwise
        """
        state = self._states.get(server_name)
        return state.is_connected if state else False

    def can_attempt_connection(self, server_name: str) -> bool:
        """
        Check if connection attempt is allowed (circuit breaker not open).

        Args:
            server_name: Name of the server

        Returns:
            True if connection can be attempted
        """
        state = self._states.get(server_name)
        if not state:
            return True  # Unknown server, allow attempt
        return state.circuit_breaker.can_execute()

    def mark_connected(self, server_name: str) -> None:
        """
        Mark a server as successfully connected.

        Args:
            server_name: Name of the server
        """
        state = self._states.get(server_name)
        if state:
            state.record_connection_success()
            logger.info("Server '%s' connected", server_name)

    def mark_failed(self, server_name: str, error: str) -> None:
        """
        Mark a server connection as failed.

        Args:
            server_name: Name of the server
            error: Error message
        """
        state = self._states.get(server_name)
        if state:
            state.record_connection_failure(error)
            logger.warning("Server '%s' connection failed: %s", server_name, error)

    def get_connection_lock(self, server_name: str) -> asyncio.Lock:
        """
        Get lock for serializing connection attempts to a server.

        Prevents multiple concurrent connection attempts to the same server.

        Args:
            server_name: Name of the server

        Returns:
            asyncio.Lock for the server
        """
        if server_name not in self._connection_locks:
            self._connection_locks[server_name] = asyncio.Lock()
        return self._connection_locks[server_name]

    def get_all_states(self) -> dict[str, dict[str, Any]]:
        """
        Get connection states for all registered servers.

        Returns:
            Dict mapping server names to state information
        """
        return {
            name: {
                "is_connected": state.is_connected,
                "configured_at": state.configured_at.isoformat(),
                "connected_at": state.connected_at.isoformat() if state.connected_at else None,
                "last_attempt_at": (
                    state.last_attempt_at.isoformat() if state.last_attempt_at else None
                ),
                "last_error": state.last_error,
                "connection_attempts": state.connection_attempts,
                "circuit_state": state.circuit_breaker.state.value,
                "circuit_failures": state.circuit_breaker.failure_count,
            }
            for name, state in self._states.items()
        }


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker prevents connection attempt."""

    def __init__(self, server_name: str, recovery_in: float):
        self.server_name = server_name
        self.recovery_in = recovery_in
        super().__init__(
            f"Circuit breaker open for '{server_name}'. Recovery attempt in {recovery_in:.1f}s"
        )
