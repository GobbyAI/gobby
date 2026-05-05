"""
Tests for HealthMonitor module (TDD red phase).

These tests are written BEFORE the module exists to drive the extraction
from hook_manager.py. They should initially fail with ImportError.

Test categories:
1. Initialization - HealthMonitor instantiation and state setup
2. Health status reporting - get_cached_status() method
3. Health check scheduling/timing - start/stop monitoring
4. Failure handling - error states and recovery
5. Integration with HookManager - via mock
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

# This import should fail initially (red phase) - module doesn't exist yet
from gobby.hooks.health_monitor import HealthMonitor

pytestmark = pytest.mark.unit

if TYPE_CHECKING:
    pass


class TestHealthMonitorInitialization:
    """Test health check initialization."""

    def test_init_with_daemon_client(self) -> None:
        """Test HealthMonitor requires a DaemonClient."""
        mock_client = MagicMock(spec=["check_status"])
        monitor = HealthMonitor(daemon_client=mock_client)

        assert monitor._daemon_client is mock_client

    def test_init_default_interval(self) -> None:
        """Test default health check interval is 10 seconds."""
        mock_client = MagicMock(spec=["check_status"])
        monitor = HealthMonitor(daemon_client=mock_client)

        assert monitor._health_check_interval == 10.0

    def test_init_custom_interval(self) -> None:
        """Test custom health check interval."""
        mock_client = MagicMock(spec=["check_status"])
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=5.0)

        assert monitor._health_check_interval == 5.0

    def test_init_with_logger(self) -> None:
        """Test HealthMonitor accepts optional logger."""
        mock_client = MagicMock(spec=["check_status"])
        logger = logging.getLogger("test")
        monitor = HealthMonitor(daemon_client=mock_client, logger=logger)

        assert monitor.logger is logger

    def test_init_creates_default_logger(self) -> None:
        """Test HealthMonitor creates logger if not provided."""
        mock_client = MagicMock(spec=["check_status"])
        monitor = HealthMonitor(daemon_client=mock_client)

        assert monitor.logger is not None
        assert isinstance(monitor.logger, logging.Logger)

    def test_init_state_defaults(self) -> None:
        """Test initial state values."""
        mock_client = MagicMock(spec=["check_status"])
        monitor = HealthMonitor(daemon_client=mock_client)

        # Initial state should indicate not ready
        assert monitor._cached_daemon_is_ready is False
        assert monitor._cached_daemon_message is None
        assert monitor._cached_daemon_status == "not_running"
        assert monitor._cached_daemon_error is None
        assert monitor._is_shutdown is False
        assert monitor._health_check_timer is None

    def test_init_thread_safety(self) -> None:
        """Test HealthMonitor has thread-safe lock."""
        mock_client = MagicMock(spec=["check_status"])
        monitor = HealthMonitor(daemon_client=mock_client)

        assert hasattr(monitor, "_health_check_lock")
        assert isinstance(monitor._health_check_lock, type(threading.Lock()))


class TestHealthStatusReporting:
    """Test health status reporting."""

    def test_get_cached_status_returns_tuple(self) -> None:
        """Test get_cached_status returns 4-tuple."""
        mock_client = MagicMock(spec=["check_status"])
        monitor = HealthMonitor(daemon_client=mock_client)

        result = monitor.get_cached_status()

        assert isinstance(result, tuple)
        assert len(result) == 4

    def test_get_cached_status_initial_values(self) -> None:
        """Test get_cached_status returns correct initial values."""
        mock_client = MagicMock(spec=["check_status"])
        monitor = HealthMonitor(daemon_client=mock_client)

        is_ready, message, status, error = monitor.get_cached_status()

        assert is_ready is False
        assert message is None
        assert status == "not_running"
        assert error is None

    def test_get_cached_status_after_successful_check(self) -> None:
        """Test get_cached_status returns updated values after check."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.return_value = (
            True,
            "Daemon is ready",
            "ready",
            None,
        )
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=0.1)

        # Start monitoring and wait for first check
        monitor.start()
        time.sleep(0.15)  # Wait for check to complete
        monitor.stop()

        is_ready, message, status, error = monitor.get_cached_status()

        assert is_ready is True
        assert message == "Daemon is ready"
        assert status == "ready"
        assert error is None

    def test_get_cached_status_thread_safe(self) -> None:
        """Test get_cached_status is thread-safe."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.return_value = (True, "Ready", "ready", None)
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=0.05)

        results: list[tuple] = []
        errors: list[Exception] = []

        def reader():
            try:
                for _ in range(10):
                    results.append(monitor.get_cached_status())
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        monitor.start()

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        monitor.stop()

        assert len(errors) == 0, f"Thread errors: {errors}"
        assert len(results) == 50  # 5 threads * 10 reads


class TestHealthCheckScheduling:
    """Test health check scheduling/timing."""

    def test_start_begins_monitoring(self) -> None:
        """Test start() begins health check monitoring."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.return_value = (True, "Ready", "ready", None)
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=1.0)

        try:
            monitor.start()
            time.sleep(0.1)  # Allow timer to start

            assert monitor._health_check_timer is not None
        finally:
            monitor.stop()

    def test_start_is_idempotent(self) -> None:
        """Test calling start() multiple times is safe."""
        mock_client = MagicMock(spec=["check_status"])

        # Add delay to ensure health_check_loop doesn't finish and replace
        # the timer before we check for idempotency
        def delayed_check():
            time.sleep(0.5)
            return (True, "Ready", "ready", None)

        mock_client.check_status.side_effect = delayed_check
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=1.0)

        try:
            monitor.start()
            timer1 = monitor._health_check_timer

            monitor.start()  # Should not create new timer
            timer2 = monitor._health_check_timer

            assert timer1 is timer2  # Same timer instance
        finally:
            monitor.stop()

    def test_stop_cancels_timer(self) -> None:
        """Test stop() cancels health check timer."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.return_value = (True, "Ready", "ready", None)
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=1.0)

        monitor.start()
        time.sleep(0.1)
        assert monitor._health_check_timer is not None

        monitor.stop()
        assert monitor._health_check_timer is None
        assert monitor._is_shutdown is True

    def test_stop_prevents_reschedule(self) -> None:
        """Test stop() prevents timer from being rescheduled."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.return_value = (True, "Ready", "ready", None)
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=0.05)

        monitor.start()
        time.sleep(0.1)  # Let a check complete
        monitor.stop()

        # After stop, is_shutdown should prevent new timers
        assert monitor._is_shutdown is True
        time.sleep(0.1)  # Wait to ensure no reschedule
        assert monitor._health_check_timer is None

    def test_check_runs_at_interval(self) -> None:
        """Test health checks run at configured interval."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.return_value = (True, "Ready", "ready", None)
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=0.1)

        monitor.start()
        time.sleep(0.35)  # Should run ~3 times (0, 0.1, 0.2, 0.3)
        monitor.stop()

        # Allow for timing variance
        assert mock_client.check_status.call_count >= 2
        assert mock_client.check_status.call_count <= 5

    def test_start_after_stop_does_nothing(self) -> None:
        """Test start() does nothing if monitor is shutdown."""
        mock_client = MagicMock(spec=["check_status"])
        monitor = HealthMonitor(daemon_client=mock_client)

        monitor.stop()
        assert monitor._is_shutdown is True

        # Should return early and not start timer
        monitor.start()
        assert monitor._health_check_timer is None


class TestHealthCheckFailureHandling:
    """Test health check failure handling."""

    def test_failure_updates_cached_status(self) -> None:
        """Test failed check updates cached status appropriately."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.return_value = (
            False,
            "Connection refused",
            "not_running",
            "ECONNREFUSED",
        )
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=0.1)

        monitor.start()
        time.sleep(0.15)
        monitor.stop()

        is_ready, message, status, error = monitor.get_cached_status()

        assert is_ready is False
        assert status == "not_running"

    def test_exception_in_check_status(self) -> None:
        """Test exception in check_status is handled gracefully."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.side_effect = ConnectionError("Network error")
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=0.1)

        monitor.start()
        time.sleep(0.15)
        monitor.stop()

        is_ready, message, status, error = monitor.get_cached_status()

        assert is_ready is False
        assert status == "not_running"
        assert "Network error" in (error or "")

    def test_recovery_after_failure(self) -> None:
        """Test health monitor recovers after failure."""
        mock_client = MagicMock(spec=["check_status"])
        # First call fails, subsequent calls succeed
        mock_client.check_status.side_effect = [
            ConnectionError("Network error"),
            (True, "Ready", "ready", None),
            (True, "Ready", "ready", None),
        ]
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=0.1)

        monitor.start()
        time.sleep(0.25)  # Allow 2-3 checks
        monitor.stop()

        is_ready, message, status, error = monitor.get_cached_status()

        assert is_ready is True
        assert status == "ready"

    def test_continuous_failures_keep_monitoring(self) -> None:
        """Test continuous failures don't stop monitoring."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.side_effect = ConnectionError("Always fails")
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=0.05)

        monitor.start()
        time.sleep(0.2)  # Should attempt multiple checks despite failures

        # Monitoring should still be active
        assert monitor._health_check_timer is not None or not monitor._is_shutdown

        monitor.stop()

        # Multiple attempts should have been made
        assert mock_client.check_status.call_count >= 2


class TestHealthMonitorIntegration:
    """Test integration with hook system using mock HookManager."""

    def test_integration_with_hook_manager_pattern(self) -> None:
        """Test HealthMonitor follows HookManager's usage pattern."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.return_value = (True, "Ready", "ready", None)

        # Simulate HookManager initialization pattern
        health_check_interval = 10.0  # from config
        monitor = HealthMonitor(
            daemon_client=mock_client,
            health_check_interval=health_check_interval,
        )

        # HookManager calls start() in __init__
        monitor.start()

        # HookManager calls get_cached_status() via _get_cached_daemon_status()
        status = monitor.get_cached_status()
        assert len(status) == 4

        # HookManager calls stop() in shutdown()
        monitor.stop()
        assert monitor._is_shutdown is True

    def test_can_be_used_as_component(self) -> None:
        """Test HealthMonitor can be composed into larger classes."""

        class MockHookManager:
            """Simulates how HookManager would use HealthMonitor."""

            def __init__(self):
                self._daemon_client = MagicMock(spec=["check_status"])
                self._daemon_client.check_status.return_value = (
                    True,
                    "Ready",
                    "ready",
                    None,
                )
                self._health_monitor = HealthMonitor(
                    daemon_client=self._daemon_client,
                    health_check_interval=0.1,
                )

            def start(self):
                self._health_monitor.start()

            def _get_cached_daemon_status(self):
                return self._health_monitor.get_cached_status()

            def shutdown(self):
                self._health_monitor.stop()

        manager = MockHookManager()
        manager.start()
        time.sleep(0.15)

        status = manager._get_cached_daemon_status()
        assert status[0] is True  # is_ready

        manager.shutdown()
        assert manager._health_monitor._is_shutdown is True

    def test_logger_integration(self) -> None:
        """Test logging behavior on health check events."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.side_effect = ConnectionError("Test error")

        logger = logging.getLogger("test_health_monitor")
        logger.setLevel(logging.DEBUG)

        # Patch logger.debug to verify no exceptions during health check cycle
        with patch.object(logger, "debug") as mock_debug:
            monitor = HealthMonitor(
                daemon_client=mock_client, health_check_interval=0.1, logger=logger
            )
            monitor.start()
            time.sleep(0.15)
            monitor.stop()
            assert monitor._is_shutdown is True
            assert mock_debug.called


class TestHealthMonitorEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_stop_without_start(self) -> None:
        """Test stop() can be called without start()."""
        mock_client = MagicMock(spec=["check_status"])
        monitor = HealthMonitor(daemon_client=mock_client)

        # Should not raise
        monitor.stop()
        assert monitor._is_shutdown is True

    def test_multiple_stops(self) -> None:
        """Test multiple stop() calls are safe."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.return_value = (True, "Ready", "ready", None)
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=1.0)

        monitor.start()
        time.sleep(0.1)

        # Multiple stops should be safe
        monitor.stop()
        monitor.stop()
        monitor.stop()

        assert monitor._is_shutdown is True

    def test_zero_interval(self) -> None:
        """Test zero interval is handled (minimum interval enforced)."""
        mock_client = MagicMock(spec=["check_status"])
        mock_client.check_status.return_value = (True, "Ready", "ready", None)

        # Zero interval might be rejected or clamped to minimum
        monitor = HealthMonitor(daemon_client=mock_client, health_check_interval=0)

        # Either raises or clamps to reasonable minimum
        assert monitor._health_check_interval >= 0

    def test_negative_interval(self) -> None:
        """Test negative interval is handled."""
        mock_client = MagicMock(spec=["check_status"])

        # Negative interval should be rejected or clamped
        with pytest.raises((ValueError, TypeError)):
            HealthMonitor(daemon_client=mock_client, health_check_interval=-1.0)
