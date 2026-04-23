"""
OpenTelemetry metric instruments.
Provides OTel-based instruments.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import psutil
from opentelemetry import metrics

logger = logging.getLogger(__name__)


class TelemetryMetrics:
    """
    OpenTelemetry-based metrics collector.

    Provides standard OTel instruments while maintaining backward compatibility
    with the legacy API for status reporting.
    """

    def __init__(self, meter: metrics.Meter) -> None:
        """Initialize telemetry metrics."""
        self._meter = meter
        self._lock = threading.Lock()
        self._start_time = time.time()

        # OTel instruments
        self._counters: dict[str, metrics.Counter] = {}
        self._up_down_counters: dict[str, metrics.UpDownCounter] = {}
        self._histograms: dict[str, metrics.Histogram] = {}

        # Legacy value tracking for /admin/status (doesn't track labels)
        self._values: dict[str, dict[str, Any]] = {
            "counters": {},
            "gauges": {},
            "histograms": {},
        }

        self._initialize_instruments()

    def _initialize_instruments(self) -> None:
        """Register all metric instruments."""
        # HTTP request metrics
        self._register_counter(
            "http_requests_total",
            "Total number of HTTP requests received",
        )
        self._register_counter(
            "http_requests_errors_total",
            "Total number of HTTP requests that resulted in errors",
        )
        self._register_histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds",
        )
        self._register_counter(
            "session_registrations_total",
            "Total number of session registration requests",
        )
        self._register_counter(
            "statusline_posts_succeeded_total",
            "Total number of successful statusline usage posts",
        )
        self._register_counter(
            "statusline_usage_gap_warnings_total",
            "Total number of statusline usage gap warnings",
        )

        # Memory operation metrics
        self._register_counter(
            "memory_saves_total",
            "Total number of memory save requests",
        )
        self._register_counter(
            "memory_saves_succeeded_total",
            "Total number of successful memory saves",
        )
        self._register_counter(
            "memory_saves_failed_total",
            "Total number of failed memory saves",
        )
        self._register_histogram(
            "memory_save_duration_seconds",
            "Memory save operation duration in seconds",
        )

        # Context restore metrics
        self._register_counter(
            "context_restores_total",
            "Total number of context restore requests",
        )
        self._register_counter(
            "context_restores_succeeded_total",
            "Total number of successful context restores",
        )
        self._register_counter(
            "context_restores_failed_total",
            "Total number of failed context restores",
        )
        self._register_histogram(
            "context_restore_duration_seconds",
            "Context restore operation duration in seconds",
        )

        # MCP call metrics
        self._register_counter(
            "mcp_calls_total",
            "Total number of MCP calls made",
        )
        self._register_counter(
            "mcp_calls_succeeded_total",
            "Total number of successful MCP calls",
        )
        self._register_counter(
            "mcp_calls_failed_total",
            "Total number of failed MCP calls",
        )
        self._register_histogram(
            "mcp_call_duration_seconds",
            "MCP call duration in seconds",
        )
        self._register_up_down_counter(
            "mcp_active_connections",
            "Number of active MCP connections",
        )

        # MCP tool call metrics
        self._register_counter(
            "mcp_tool_calls_total",
            "Total number of MCP tool calls made",
        )
        self._register_counter(
            "mcp_tool_calls_succeeded_total",
            "Total number of successful MCP tool calls",
        )
        self._register_counter(
            "mcp_tool_calls_failed_total",
            "Total number of failed MCP tool calls",
        )
        self._register_histogram(
            "mcp_tool_call_duration_seconds",
            "MCP tool call duration in seconds",
        )

        # Background task metrics
        self._register_up_down_counter(
            "background_tasks_active",
            "Number of currently active background tasks",
        )
        self._register_counter(
            "background_tasks_total",
            "Total number of background tasks created",
        )
        self._register_counter(
            "background_tasks_completed_total",
            "Total number of background tasks completed",
        )
        self._register_counter(
            "background_tasks_failed_total",
            "Total number of background tasks that failed",
        )

        # Daemon health metrics (using ObservableGauges)
        self._meter.create_observable_gauge(
            "daemon_uptime_seconds",
            callbacks=[self._observe_gauge_callback("daemon_uptime_seconds")],
            description="Daemon uptime in seconds",
        )
        self._meter.create_observable_gauge(
            "daemon_memory_usage_bytes",
            callbacks=[self._observe_gauge_callback("daemon_memory_usage_bytes")],
            description="Daemon memory usage in bytes",
        )
        self._meter.create_observable_gauge(
            "daemon_cpu_percent",
            callbacks=[self._observe_gauge_callback("daemon_cpu_percent")],
            description="Daemon CPU usage percentage",
        )

        # MCP tool listing metrics
        self._register_histogram(
            "list_mcp_tools",
            "Time to list MCP tools",
        )

        # Shutdown metrics
        self._register_counter(
            "shutdown_succeeded_total",
            "Successful daemon shutdowns",
        )
        self._register_counter(
            "shutdown_failed_total",
            "Failed daemon shutdowns",
        )

        # Hook execution metrics
        self._register_counter(
            "hooks_total",
            "Total number of hook executions",
        )
        self._register_counter(
            "hooks_succeeded_total",
            "Total number of successful hook executions",
        )
        self._register_counter(
            "hooks_failed_total",
            "Total number of failed hook executions",
        )

    def _register_counter(self, name: str, description: str) -> None:
        self._counters[name] = self._meter.create_counter(name, unit="1", description=description)
        self._values["counters"][name] = {"value": 0, "labels": {}}

    def _register_up_down_counter(self, name: str, description: str) -> None:
        self._up_down_counters[name] = self._meter.create_up_down_counter(
            name, unit="1", description=description
        )
        self._values["gauges"][name] = {"value": 0, "labels": {}}

    def _register_histogram(self, name: str, description: str) -> None:
        self._histograms[name] = self._meter.create_histogram(
            name, unit="s", description=description
        )
        self._values["histograms"][name] = {"count": 0, "sum": 0.0, "buckets": {}, "labels": {}}

    def _observe_gauge_callback(self, name: str) -> Callable[..., Any]:
        def callback(options: metrics.CallbackOptions) -> list[metrics.Observation]:
            with self._lock:
                val = self._values["gauges"].get(name, {}).get("value", 0.0)
                return [metrics.Observation(val)]

        return callback

    def inc_counter(
        self, name: str, amount: int = 1, attributes: dict[str, Any] | None = None
    ) -> None:
        """Increment counter by amount."""
        if name in self._counters:
            self._counters[name].add(amount, attributes or {})
            with self._lock:
                self._values["counters"][name]["value"] += amount
        else:
            logger.warning(f"Counter {name} not registered")

    def set_gauge(self, name: str, value: float, attributes: dict[str, Any] | None = None) -> None:
        """Set a gauge to value."""
        # For ObservableGauges and UpDownCounters, we store the value
        with self._lock:
            # If it's an UpDownCounter, we need to add the delta
            if name in self._up_down_counters:
                old_value = self._values["gauges"][name]["value"]
                delta = value - old_value
                self._up_down_counters[name].add(delta, attributes or {})
                self._values["gauges"][name]["value"] = value
            else:
                # ObservableGauge or untracked
                self._values["gauges"].setdefault(name, {"value": 0.0, "labels": {}})
                self._values["gauges"][name]["value"] = value

    def inc_gauge(
        self, name: str, amount: float = 1.0, attributes: dict[str, Any] | None = None
    ) -> None:
        """Increment a gauge by amount."""
        if name in self._up_down_counters:
            self._up_down_counters[name].add(amount, attributes or {})
            with self._lock:
                self._values["gauges"][name]["value"] += amount
        else:
            with self._lock:
                self._values["gauges"].setdefault(name, {"value": 0.0, "labels": {}})
                self._values["gauges"][name]["value"] += amount

    def dec_gauge(
        self, name: str, amount: float = 1.0, attributes: dict[str, Any] | None = None
    ) -> None:
        """Decrement a gauge by amount."""
        self.inc_gauge(name, -amount, attributes)

    def observe_histogram(
        self, name: str, value: float, attributes: dict[str, Any] | None = None
    ) -> None:
        """Record an observation in a histogram."""
        if name in self._histograms:
            self._histograms[name].record(value, attributes or {})
            with self._lock:
                hist = self._values["histograms"][name]
                hist["count"] += 1
                hist["sum"] += value
        else:
            logger.warning(f"Histogram {name} not registered")

    def get_uptime(self) -> float:
        """Get collector uptime in seconds."""
        return time.time() - self._start_time

    def update_daemon_metrics(self, pid: int | None = None) -> None:
        """Update daemon health metrics (uptime, memory, CPU)."""
        try:
            # Get process
            process = psutil.Process(pid) if pid else psutil.Process(os.getpid())

            # Update uptime
            self.set_gauge("daemon_uptime_seconds", self.get_uptime())

            # Update memory usage
            mem_info = process.memory_info()
            self.set_gauge("daemon_memory_usage_bytes", float(mem_info.rss))

            # Update CPU usage
            cpu_percent = process.cpu_percent(interval=None)
            self.set_gauge("daemon_cpu_percent", cpu_percent)

        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.warning(f"Failed to update daemon metrics: {e}")

    def get_all_metrics(self) -> dict[str, Any]:
        """Get all metrics for backward compatibility with /admin/status."""
        with self._lock:
            return {
                "counters": self._values["counters"],
                "gauges": self._values["gauges"],
                "histograms": self._values["histograms"],
                "uptime_seconds": self.get_uptime(),
            }


# Global metrics collector instance
_telemetry_metrics: TelemetryMetrics | None = None


def get_telemetry_metrics() -> TelemetryMetrics:
    """Get global telemetry metrics instance."""
    global _telemetry_metrics
    if _telemetry_metrics is None:
        # Avoid circular import
        from opentelemetry import metrics

        meter = metrics.get_meter("gobby")
        _telemetry_metrics = TelemetryMetrics(meter)
    return _telemetry_metrics


def inc_counter(name: str, amount: int = 1, attributes: dict[str, Any] | None = None) -> None:
    """Increment global counter by amount."""
    get_telemetry_metrics().inc_counter(name, amount, attributes)


def set_gauge(name: str, value: float, attributes: dict[str, Any] | None = None) -> None:
    """Set global gauge to value."""
    get_telemetry_metrics().set_gauge(name, value, attributes)


def inc_gauge(name: str, amount: float = 1.0, attributes: dict[str, Any] | None = None) -> None:
    """Increment global gauge by amount."""
    get_telemetry_metrics().inc_gauge(name, amount, attributes)


def dec_gauge(name: str, amount: float = 1.0, attributes: dict[str, Any] | None = None) -> None:
    """Decrement global gauge by amount."""
    get_telemetry_metrics().dec_gauge(name, amount, attributes)


def observe_histogram(name: str, value: float, attributes: dict[str, Any] | None = None) -> None:
    """Record observation in global histogram."""
    get_telemetry_metrics().observe_histogram(name, value, attributes)


def get_all_metrics() -> dict[str, Any]:
    """Get all metrics for backward compatibility with /admin/status."""
    return get_telemetry_metrics().get_all_metrics()


def update_daemon_metrics(pid: int | None = None) -> None:
    """Update daemon health metrics (uptime, memory, CPU)."""
    get_telemetry_metrics().update_daemon_metrics(pid)
