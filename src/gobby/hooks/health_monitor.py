"""
Health monitor module for daemon health check monitoring.

This module is extracted from hook_manager.py using Strangler Fig pattern.
It provides background health check monitoring for the Gobby daemon.

Classes:
    HealthMonitor: Background daemon health check monitoring.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.utils.daemon_client import DaemonClient


class HealthMonitor:
    """
    Background daemon health check monitoring.

    Periodically checks daemon health via DaemonClient and caches the result
    for fast access without HTTP calls. Thread-safe.

    Extracted from HookManager to separate health monitoring concerns.
    """

    def __init__(
        self,
        daemon_client: DaemonClient,
        health_check_interval: float = 10.0,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize HealthMonitor.

        Args:
            daemon_client: DaemonClient for health checks
            health_check_interval: Interval between checks in seconds (must be >= 0)
            logger: Optional logger instance

        Raises:
            ValueError: If health_check_interval is negative
        """
        if health_check_interval < 0:
            raise ValueError("health_check_interval must be non-negative")

        self._daemon_client = daemon_client
        self._health_check_interval = health_check_interval
        self.logger = logger or logging.getLogger(__name__)

        # Cached health status
        self._cached_daemon_is_ready: bool = False
        self._cached_daemon_message: str | None = None
        self._cached_daemon_status: str = "not_running"
        self._cached_daemon_error: str | None = None

        # Threading state
        self._health_check_timer: threading.Timer | None = None
        self._health_check_lock = threading.Lock()
        self._fresh_check_condition = threading.Condition()
        self._fresh_check_in_progress = False
        self._fresh_check_waiters = 0
        self._is_shutdown: bool = False

    def start(self) -> None:
        """
        Start background health check monitoring.

        Idempotent - safe to call multiple times.
        """
        with self._health_check_lock:
            if self._health_check_timer is not None:
                return  # Already running
            if self._is_shutdown:
                return  # Already shutdown

            def health_check_loop() -> None:
                """Background health check loop."""
                try:
                    self.check_now()
                except Exception as e:
                    self.logger.debug(f"Health check failed: {e}", exc_info=True)
                finally:
                    # Schedule next check only if not shutting down
                    with self._health_check_lock:
                        if not self._is_shutdown:
                            self._health_check_timer = threading.Timer(
                                self._health_check_interval,
                                health_check_loop,
                            )
                            self._health_check_timer.daemon = True
                            self._health_check_timer.start()

            # Start first check immediately
            self._health_check_timer = threading.Timer(0, health_check_loop)
            self._health_check_timer.daemon = True
            self._health_check_timer.start()

    def stop(self) -> None:
        """
        Stop background health check monitoring.

        Cancels any pending timer and prevents new timers from being scheduled.
        Safe to call multiple times.
        """
        with self._health_check_lock:
            self._is_shutdown = True
            if self._health_check_timer is not None:
                self._health_check_timer.cancel()
                self._health_check_timer = None

    def get_cached_status(self) -> tuple[bool, str | None, str, str | None]:
        """
        Get cached daemon status without making HTTP call.

        Returns:
            Tuple of (is_ready, message, status, error) where:
            - is_ready: True if daemon is healthy
            - message: Human-readable status message
            - status: One of: "ready", "not_running", "cannot_access"
            - error: Error details if status != "ready"
        """
        with self._health_check_lock:
            return (
                self._cached_daemon_is_ready,
                self._cached_daemon_message,
                self._cached_daemon_status,
                self._cached_daemon_error,
            )

    def check_now(self) -> bool:
        """
        Perform immediate health check (not cached).

        Makes a fresh HTTP call to check daemon status and updates the cache.
        Used for retry logic when cached status indicates daemon is unavailable.

        Returns:
            True if daemon is healthy, False otherwise
        """
        with self._fresh_check_condition:
            if self._fresh_check_in_progress:
                self._fresh_check_waiters += 1
                try:
                    while self._fresh_check_in_progress:
                        self._fresh_check_condition.wait()
                finally:
                    self._fresh_check_waiters -= 1
                with self._health_check_lock:
                    return self._cached_daemon_is_ready
            self._fresh_check_in_progress = True

        try:
            try:
                is_ready, message, status, error = self._daemon_client.check_status()
                with self._health_check_lock:
                    self._cached_daemon_is_ready = is_ready
                    self._cached_daemon_message = message
                    self._cached_daemon_status = status
                    self._cached_daemon_error = error
                return is_ready
            except Exception as e:
                self.logger.debug(f"Immediate health check failed: {e}")
                with self._health_check_lock:
                    self._cached_daemon_is_ready = False
                    self._cached_daemon_status = "not_running"
                    self._cached_daemon_error = str(e)
                return False
        finally:
            with self._fresh_check_condition:
                self._fresh_check_in_progress = False
                self._fresh_check_condition.notify_all()


__all__ = ["HealthMonitor"]
