"""Tests for lazy server initialization."""

import asyncio
import time

import pytest

from gobby.mcp_proxy.lazy import (
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitState,
    LazyConnectionState,
    LazyServerConnector,
    RetryConfig,
)

pytestmark = pytest.mark.unit


class TestCircuitBreaker:
    """Tests for CircuitBreaker."""

    def test_initial_state_is_closed(self) -> None:
        """Circuit starts in closed state."""
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0
        assert cb.can_execute()

    def test_success_resets_failures(self) -> None:
        """Success resets failure count."""
        cb = CircuitBreaker()
        cb.record_failure()
        cb.record_failure()
        assert cb.failure_count == 2

        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == CircuitState.CLOSED

    def test_opens_after_threshold_failures(self) -> None:
        """Circuit opens after failure threshold."""
        cb = CircuitBreaker(failure_threshold=3)

        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

        cb.record_failure()  # Third failure trips circuit
        assert cb.state == CircuitState.OPEN
        assert not cb.can_execute()

    def test_recovery_timeout_triggers_half_open(self) -> None:
        """After recovery timeout, circuit enters half-open."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)

        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert not cb.can_execute()

        # Wait for recovery timeout
        time.sleep(0.15)

        assert cb.can_execute()
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_closes_circuit(self) -> None:
        """Success in half-open state closes circuit."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)

        cb.record_failure()
        time.sleep(0.02)
        cb.can_execute()  # Triggers half-open

        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute()

    def test_half_open_failure_reopens_circuit(self) -> None:
        """Failure in half-open state reopens circuit."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)

        cb.record_failure()
        time.sleep(0.02)
        cb.can_execute()  # Triggers half-open

        cb.record_failure()
        assert cb.state == CircuitState.OPEN


class TestRetryConfig:
    """Tests for RetryConfig."""

    def test_exponential_delays(self) -> None:
        """Delays increase exponentially."""
        config = RetryConfig(initial_delay=1.0, multiplier=2.0, max_delay=16.0)

        assert config.get_delay(0) == 1.0
        assert config.get_delay(1) == 2.0
        assert config.get_delay(2) == 4.0
        assert config.get_delay(3) == 8.0
        assert config.get_delay(4) == 16.0

    def test_delay_capped_at_max(self) -> None:
        """Delay doesn't exceed max_delay."""
        config = RetryConfig(initial_delay=1.0, multiplier=2.0, max_delay=5.0)

        assert config.get_delay(5) == 5.0
        assert config.get_delay(10) == 5.0


class TestLazyConnectionState:
    """Tests for LazyConnectionState."""

    def test_initial_state(self) -> None:
        """Initial state is not connected."""
        state = LazyConnectionState()
        assert not state.is_connected
        assert state.connected_at is None
        assert state.last_error is None
        assert state.connection_attempts == 0

    def test_connection_attempt_tracking(self) -> None:
        """Connection attempts are tracked."""
        state = LazyConnectionState()

        state.record_connection_attempt()
        assert state.connection_attempts == 1
        assert state.last_attempt_at is not None

        state.record_connection_attempt()
        assert state.connection_attempts == 2

    def test_connection_success(self) -> None:
        """Successful connection is recorded."""
        state = LazyConnectionState()

        state.record_connection_success()
        assert state.is_connected
        assert state.connected_at is not None
        assert state.last_error is None

    def test_connection_failure(self) -> None:
        """Failed connection is recorded."""
        state = LazyConnectionState()

        state.record_connection_failure("Connection refused")
        assert not state.is_connected
        assert state.last_error == "Connection refused"


class TestLazyServerConnector:
    """Tests for LazyServerConnector."""

    def test_register_server(self) -> None:
        """Servers can be registered."""
        connector = LazyServerConnector()

        connector.register_server("test-server")
        assert connector.get_state("test-server") is not None
        assert not connector.is_connected("test-server")

    def test_unregister_server(self) -> None:
        """Servers can be unregistered."""
        connector = LazyServerConnector()

        connector.register_server("test-server")
        connector.unregister_server("test-server")
        assert connector.get_state("test-server") is None

    def test_is_connected(self) -> None:
        """Connection status is tracked."""
        connector = LazyServerConnector()

        connector.register_server("test-server")
        assert not connector.is_connected("test-server")

        connector.mark_connected("test-server")
        assert connector.is_connected("test-server")

    def test_mark_failed(self) -> None:
        """Failures are recorded."""
        connector = LazyServerConnector()

        connector.register_server("test-server")
        connector.mark_failed("test-server", "Connection timeout")

        state = connector.get_state("test-server")
        assert state is not None
        assert state.last_error == "Connection timeout"
        assert state.circuit_breaker.failure_count == 1

    def test_circuit_breaker_blocks_connection(self) -> None:
        """Circuit breaker blocks connection attempts."""
        connector = LazyServerConnector(circuit_breaker_config={"failure_threshold": 2})

        connector.register_server("test-server")
        assert connector.can_attempt_connection("test-server")

        connector.mark_failed("test-server", "Error 1")
        assert connector.can_attempt_connection("test-server")

        connector.mark_failed("test-server", "Error 2")
        assert not connector.can_attempt_connection("test-server")

    def test_get_all_states(self) -> None:
        """All states can be retrieved."""
        connector = LazyServerConnector()

        connector.register_server("server-1")
        connector.register_server("server-2")
        connector.mark_connected("server-1")

        states = connector.get_all_states()
        assert len(states) == 2
        assert states["server-1"]["is_connected"] is True
        assert states["server-2"]["is_connected"] is False

    @pytest.mark.asyncio
    async def test_connection_lock_prevents_concurrent_connects(self):
        """Connection lock serializes connection attempts."""
        connector = LazyServerConnector()
        connector.register_server("test-server")

        lock = connector.get_connection_lock("test-server")

        # Acquire lock
        async with lock:
            # Try to acquire again (should fail or wait)
            acquired = lock.locked()
            assert acquired


class TestCircuitBreakerOpen:
    """Tests for CircuitBreakerOpen exception."""

    def test_exception_message(self) -> None:
        """Exception has descriptive message."""
        exc = CircuitBreakerOpen("test-server", 15.5)

        assert exc.server_name == "test-server"
        assert exc.recovery_in == 15.5
        assert "test-server" in str(exc)
        assert "15.5" in str(exc)


class TestCircuitBreakerEdgeCases:
    """Additional edge case tests for CircuitBreaker."""

    def test_open_state_with_no_last_failure_time(self) -> None:
        """Circuit breaker allows execution when open but no last_failure_time recorded."""
        cb = CircuitBreaker()
        # Manually set to OPEN state without going through record_failure
        cb.state = CircuitState.OPEN
        cb.last_failure_time = None

        # Should return True when last_failure_time is None (line 81)
        assert cb.can_execute() is True

    def test_half_open_limits_concurrent_calls(self) -> None:
        """Half-open state limits number of test calls."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01, half_open_max_calls=2)

        # Trip the circuit
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Wait for recovery and trigger half-open
        time.sleep(0.02)
        # First can_execute() transitions from OPEN to HALF_OPEN and returns True
        # but doesn't count as a half_open call (it's the transition call)
        assert cb.can_execute() is True
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.half_open_calls == 0  # Reset to 0 on transition

        # Now in HALF_OPEN state, calls increment half_open_calls
        assert cb.can_execute() is True
        assert cb.half_open_calls == 1

        # Second call in half-open should succeed
        assert cb.can_execute() is True
        assert cb.half_open_calls == 2

        # Third call should be blocked (exceeds half_open_max_calls)
        assert cb.can_execute() is False

    def test_half_open_state_blocks_after_max_calls(self) -> None:
        """Half-open state blocks execution after max calls reached."""
        cb = CircuitBreaker(half_open_max_calls=1)
        cb.state = CircuitState.HALF_OPEN
        cb.half_open_calls = 1  # Already at max

        # Should return False when half_open_calls >= half_open_max_calls (lines 91-95)
        assert cb.can_execute() is False

    def test_can_execute_returns_false_for_unknown_state(self) -> None:
        """can_execute returns False for unexpected states."""
        cb = CircuitBreaker()
        # This tests the final return False (line 97) - though in practice
        # all enum states are covered, we test the logic flow
        cb.state = CircuitState.HALF_OPEN
        cb.half_open_calls = 5  # More than max
        assert cb.can_execute() is False

    def test_half_open_failure_resets_half_open_calls(self) -> None:
        """Failure in half-open resets the half_open_calls counter."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)

        # Trip circuit and enter half-open
        cb.record_failure()
        time.sleep(0.02)
        cb.can_execute()  # Transitions to HALF_OPEN, resets half_open_calls to 0
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.half_open_calls == 0  # Reset on transition

        # Make a test call (increments half_open_calls)
        cb.can_execute()
        assert cb.half_open_calls == 1

        # Fail in half-open state
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.half_open_calls == 0

    def test_success_resets_half_open_calls(self) -> None:
        """Success resets half_open_calls counter."""
        cb = CircuitBreaker()
        cb.state = CircuitState.HALF_OPEN
        cb.half_open_calls = 3

        cb.record_success()
        assert cb.half_open_calls == 0
        assert cb.state == CircuitState.CLOSED


class TestLazyServerConnectorEdgeCases:
    """Additional edge case tests for LazyServerConnector."""

    def test_register_server_idempotent(self) -> None:
        """Registering same server twice does not create duplicate state."""
        connector = LazyServerConnector()

        connector.register_server("test-server")
        state1 = connector.get_state("test-server")
        state1.connection_attempts = 5  # Modify to detect if replaced

        # Register again
        connector.register_server("test-server")
        state2 = connector.get_state("test-server")

        # Should be same state object (not replaced)
        assert state2.connection_attempts == 5
        assert state1 is state2

    def test_can_attempt_connection_unknown_server(self) -> None:
        """Unknown server allows connection attempts."""
        connector = LazyServerConnector()
        # Server not registered - should allow attempt (line 249)
        assert connector.can_attempt_connection("unknown-server") is True

    def test_is_connected_unknown_server(self) -> None:
        """Unknown server returns False for is_connected."""
        connector = LazyServerConnector()
        assert connector.is_connected("unknown-server") is False

    def test_mark_connected_unknown_server_no_error(self) -> None:
        """Marking unknown server as connected does not raise error."""
        connector = LazyServerConnector()
        # Should not raise - just does nothing (line 260)
        connector.mark_connected("unknown-server")
        # Server should still not exist in states
        assert connector.get_state("unknown-server") is None

    def test_mark_failed_unknown_server_no_error(self) -> None:
        """Marking unknown server as failed does not raise error."""
        connector = LazyServerConnector()
        # Should not raise - just does nothing (line 273)
        connector.mark_failed("unknown-server", "Some error")
        # Server should still not exist in states
        assert connector.get_state("unknown-server") is None

    def test_get_connection_lock_creates_lock_for_unregistered(self) -> None:
        """Getting lock for unregistered server creates new lock."""
        connector = LazyServerConnector()
        # Server not registered (line 290)
        lock = connector.get_connection_lock("unregistered-server")
        assert lock is not None

        # Getting same lock again returns same instance
        lock2 = connector.get_connection_lock("unregistered-server")
        assert lock is lock2

    def test_unregister_nonexistent_server_no_error(self) -> None:
        """Unregistering non-existent server does not raise error."""
        connector = LazyServerConnector()
        connector.unregister_server("nonexistent")
        assert connector.get_state("nonexistent") is None

    def test_custom_retry_config(self) -> None:
        """Custom retry config is used."""
        custom_config = RetryConfig(max_retries=5, initial_delay=2.0)
        connector = LazyServerConnector(retry_config=custom_config)
        assert connector.retry_config.max_retries == 5
        assert connector.retry_config.initial_delay == 2.0

    def test_custom_circuit_breaker_config(self) -> None:
        """Custom circuit breaker config is applied to new servers."""
        connector = LazyServerConnector(
            circuit_breaker_config={"failure_threshold": 10, "recovery_timeout": 60.0}
        )
        connector.register_server("test-server")
        state = connector.get_state("test-server")
        assert state is not None
        assert state.circuit_breaker.failure_threshold == 10
        assert state.circuit_breaker.recovery_timeout == 60.0

    def test_get_all_states_with_various_states(self) -> None:
        """get_all_states includes all state information."""
        connector = LazyServerConnector()

        connector.register_server("connected-server")
        connector.mark_connected("connected-server")

        connector.register_server("failed-server")
        connector.mark_failed("failed-server", "Connection timeout")
        connector.mark_failed("failed-server", "Connection timeout")
        connector.mark_failed("failed-server", "Connection timeout")

        connector.register_server("fresh-server")

        states = connector.get_all_states()

        assert states["connected-server"]["is_connected"] is True
        assert states["connected-server"]["connected_at"] is not None
        assert states["connected-server"]["last_error"] is None
        assert states["connected-server"]["circuit_state"] == "closed"

        assert states["failed-server"]["is_connected"] is False
        assert states["failed-server"]["last_error"] == "Connection timeout"
        assert states["failed-server"]["circuit_failures"] == 3
        assert states["failed-server"]["circuit_state"] == "open"

        assert states["fresh-server"]["is_connected"] is False
        assert states["fresh-server"]["connected_at"] is None
        assert states["fresh-server"]["last_attempt_at"] is None


class TestLazyConnectionStateEdgeCases:
    """Additional edge case tests for LazyConnectionState."""

    def test_record_connection_success_clears_error(self) -> None:
        """Recording success clears any previous error."""
        state = LazyConnectionState()
        state.record_connection_failure("Previous error")
        assert state.last_error == "Previous error"

        state.record_connection_success()
        assert state.last_error is None
        assert state.is_connected

    def test_multiple_connection_attempts(self) -> None:
        """Multiple connection attempts are tracked correctly."""
        state = LazyConnectionState()

        for i in range(5):
            state.record_connection_attempt()
            assert state.connection_attempts == i + 1
            assert state.last_attempt_at is not None

    def test_configured_at_is_set_on_creation(self) -> None:
        """configured_at is set when state is created."""
        state = LazyConnectionState()
        assert state.configured_at is not None

    def test_circuit_breaker_failure_propagates(self) -> None:
        """Connection failure updates circuit breaker."""
        state = LazyConnectionState()
        assert state.circuit_breaker.failure_count == 0

        state.record_connection_failure("Error 1")
        assert state.circuit_breaker.failure_count == 1

        state.record_connection_failure("Error 2")
        assert state.circuit_breaker.failure_count == 2


class TestRetryConfigEdgeCases:
    """Additional edge case tests for RetryConfig."""

    def test_default_values(self) -> None:
        """Default configuration values are sensible."""
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.initial_delay == 1.0
        assert config.max_delay == 16.0
        assert config.multiplier == 2.0

    def test_zero_attempt_delay(self) -> None:
        """First attempt (0-indexed) uses initial delay."""
        config = RetryConfig(initial_delay=0.5)
        assert config.get_delay(0) == 0.5

    def test_delay_never_exceeds_max(self) -> None:
        """Delay is always capped at max_delay regardless of attempt number."""
        config = RetryConfig(initial_delay=1.0, multiplier=10.0, max_delay=100.0)
        # Attempt 10 would be 1.0 * 10^10 without cap
        assert config.get_delay(10) == 100.0

    def test_custom_multiplier(self) -> None:
        """Custom multiplier affects delay calculation."""
        config = RetryConfig(initial_delay=1.0, multiplier=3.0, max_delay=1000.0)
        assert config.get_delay(0) == 1.0
        assert config.get_delay(1) == 3.0
        assert config.get_delay(2) == 9.0
        assert config.get_delay(3) == 27.0


class TestConcurrentConnections:
    """Tests for concurrent connection handling."""

    @pytest.mark.asyncio
    async def test_multiple_servers_independent_locks(self):
        """Different servers have independent locks."""
        connector = LazyServerConnector()
        connector.register_server("server-1")
        connector.register_server("server-2")

        lock1 = connector.get_connection_lock("server-1")
        lock2 = connector.get_connection_lock("server-2")

        # Locks should be different objects
        assert lock1 is not lock2

        # Both can be acquired simultaneously
        async with lock1:
            async with lock2:
                assert lock1.locked()
                assert lock2.locked()

    @pytest.mark.asyncio
    async def test_concurrent_connection_attempts_serialized(self):
        """Concurrent connection attempts to same server are serialized."""
        connector = LazyServerConnector()
        connector.register_server("test-server")

        connection_order = []

        async def connect(name: str):
            lock = connector.get_connection_lock("test-server")
            async with lock:
                connection_order.append(f"{name}_start")
                await asyncio.sleep(0.01)
                connection_order.append(f"{name}_end")

        # Start two concurrent connections
        await asyncio.gather(connect("first"), connect("second"))

        # Connections should be serialized (one completes before other starts)
        assert connection_order[0] == "first_start" or connection_order[0] == "second_start"
        first = connection_order[0].replace("_start", "")
        second = "second" if first == "first" else "first"
        assert connection_order == [
            f"{first}_start",
            f"{first}_end",
            f"{second}_start",
            f"{second}_end",
        ]


class TestCircuitStateEnum:
    """Tests for CircuitState enum."""

    def test_enum_values(self) -> None:
        """CircuitState has expected values."""
        assert CircuitState.CLOSED.value == "closed"
        assert CircuitState.OPEN.value == "open"
        assert CircuitState.HALF_OPEN.value == "half_open"

    def test_enum_is_string(self) -> None:
        """CircuitState inherits from str."""
        assert isinstance(CircuitState.CLOSED, str)
        assert CircuitState.CLOSED == "closed"
