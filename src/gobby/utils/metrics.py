"""
Metrics collection service for Gobby Client Runtime.

Provides in-memory metrics collection for monitoring daemon and HTTP
server performance with Prometheus-compatible export format.
"""

import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Counter:
    """Simple counter metric."""

    name: str
    help_text: str
    value: int = 0
    labels: dict[str, str] = field(default_factory=dict)

    def inc(self, amount: int = 1) -> None:
        """Increment counter by amount."""
        self.value += amount


@dataclass
class Gauge:
    """Gauge metric that can go up or down."""

    name: str
    help_text: str
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def set(self, value: float) -> None:
        """Set gauge to value."""
        self.value = value

    def inc(self, amount: float = 1.0) -> None:
        """Increment gauge by amount."""
        self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        """Decrement gauge by amount."""
        self.value -= amount


@dataclass
class Histogram:
    """Histogram metric for tracking distributions."""

    name: str
    help_text: str
    buckets: list[float] = field(
        default_factory=lambda: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
    )
    bucket_counts: dict[float, int] = field(default_factory=dict)
    sum: float = 0.0
    count: int = 0
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Initialize bucket counts."""
        for bucket in self.buckets:
            self.bucket_counts[bucket] = 0

    def observe(self, value: float) -> None:
        """Record an observation."""
        self.sum += value
        self.count += 1

        # Update bucket counts
        for bucket in self.buckets:
            if value <= bucket:
                self.bucket_counts[bucket] += 1


class MetricsCollector:
    """
    In-memory metrics collector with thread-safe operations.

    Collects counters, gauges, and histograms for monitoring
    daemon health, HTTP performance, and memory operations.
    """

    def __init__(self) -> None:
        """Initialize metrics collector."""
        self._lock = Lock()
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._start_time = time.time()

        # Initialize core metrics
        self._initialize_metrics()

    def _initialize_metrics(self) -> None:
        """Initialize standard metrics."""
        # HTTP request metrics
        self.register_counter(
            "http_requests_total",
            "Total number of HTTP requests received",
        )
        self.register_counter(
            "http_requests_errors_total",
            "Total number of HTTP requests that resulted in errors",
        )
        self.register_histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds",
        )
        self.register_counter(
            "session_registrations_total",
            "Total number of session registration requests",
        )

        # Memory operation metrics
        self.register_counter(
            "memory_saves_total",
            "Total number of memory save requests",
        )
        self.register_counter(
            "memory_saves_succeeded_total",
            "Total number of successful memory saves",
        )
        self.register_counter(
            "memory_saves_failed_total",
            "Total number of failed memory saves",
        )
        self.register_histogram(
            "memory_save_duration_seconds",
            "Memory save operation duration in seconds",
        )

        # Context restore metrics
        self.register_counter(
            "context_restores_total",
            "Total number of context restore requests",
        )
        self.register_counter(
            "context_restores_succeeded_total",
            "Total number of successful context restores",
        )
        self.register_counter(
            "context_restores_failed_total",
            "Total number of failed context restores",
        )
        self.register_histogram(
            "context_restore_duration_seconds",
            "Context restore operation duration in seconds",
        )

        # MCP call metrics
        self.register_counter(
            "mcp_calls_total",
            "Total number of MCP calls made",
        )
        self.register_counter(
            "mcp_calls_succeeded_total",
            "Total number of successful MCP calls",
        )
        self.register_counter(
            "mcp_calls_failed_total",
            "Total number of failed MCP calls",
        )
        self.register_histogram(
            "mcp_call_duration_seconds",
            "MCP call duration in seconds",
        )
        self.register_gauge(
            "mcp_active_connections",
            "Number of active MCP connections",
        )

        # MCP tool call metrics (specific to tool invocations)
        self.register_counter(
            "mcp_tool_calls_total",
            "Total number of MCP tool calls made",
        )
        self.register_counter(
            "mcp_tool_calls_succeeded_total",
            "Total number of successful MCP tool calls",
        )
        self.register_counter(
            "mcp_tool_calls_failed_total",
            "Total number of failed MCP tool calls",
        )

        # Background task metrics
        self.register_gauge(
            "background_tasks_active",
            "Number of currently active background tasks",
        )
        self.register_counter(
            "background_tasks_total",
            "Total number of background tasks created",
        )
        self.register_counter(
            "background_tasks_completed_total",
            "Total number of background tasks completed",
        )
        self.register_counter(
            "background_tasks_failed_total",
            "Total number of background tasks that failed",
        )

        # Daemon health metrics
        self.register_gauge(
            "daemon_uptime_seconds",
            "Daemon uptime in seconds",
        )
        self.register_gauge(
            "daemon_memory_usage_bytes",
            "Daemon memory usage in bytes",
        )
        self.register_gauge(
            "daemon_cpu_percent",
            "Daemon CPU usage percentage",
        )

        # Hook execution metrics
        self.register_counter(
            "hooks_total",
            "Total number of hook executions",
        )
        self.register_counter(
            "hooks_succeeded_total",
            "Total number of successful hook executions",
        )
        self.register_counter(
            "hooks_failed_total",
            "Total number of failed hook executions",
        )

    def register_counter(
        self, name: str, help_text: str, labels: dict[str, str] | None = None
    ) -> Counter:
        """
        Register a new counter metric.

        Args:
            name: Metric name
            help_text: Description of what this metric measures
            labels: Optional labels for this metric

        Returns:
            Counter instance
        """
        with self._lock:
            if name in self._counters:
                return self._counters[name]

            counter = Counter(name=name, help_text=help_text, labels=labels or {})
            self._counters[name] = counter
            return counter

    def register_gauge(
        self, name: str, help_text: str, labels: dict[str, str] | None = None
    ) -> Gauge:
        """
        Register a new gauge metric.

        Args:
            name: Metric name
            help_text: Description of what this metric measures
            labels: Optional labels for this metric

        Returns:
            Gauge instance
        """
        with self._lock:
            if name in self._gauges:
                return self._gauges[name]

            gauge = Gauge(name=name, help_text=help_text, labels=labels or {})
            self._gauges[name] = gauge
            return gauge

    def register_histogram(
        self,
        name: str,
        help_text: str,
        buckets: list[float] | None = None,
        labels: dict[str, str] | None = None,
    ) -> Histogram:
        """
        Register a new histogram metric.

        Args:
            name: Metric name
            help_text: Description of what this metric measures
            buckets: Histogram buckets (defaults to standard durations)
            labels: Optional labels for this metric

        Returns:
            Histogram instance
        """
        with self._lock:
            if name in self._histograms:
                return self._histograms[name]

            histogram = Histogram(
                name=name,
                help_text=help_text,
                buckets=buckets or [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
                labels=labels or {},
            )
            self._histograms[name] = histogram
            return histogram

    def inc_counter(self, name: str, amount: int = 1) -> None:
        """
        Increment a counter by amount.

        Args:
            name: Counter name
            amount: Amount to increment by (default: 1)
        """
        with self._lock:
            if name in self._counters:
                self._counters[name].inc(amount)
            else:
                logger.warning(f"Counter {name} not registered")

    def set_gauge(self, name: str, value: float) -> None:
        """
        Set a gauge to value.

        Args:
            name: Gauge name
            value: Value to set
        """
        with self._lock:
            if name in self._gauges:
                self._gauges[name].set(value)
            else:
                logger.warning(f"Gauge {name} not registered")

    def inc_gauge(self, name: str, amount: float = 1.0) -> None:
        """
        Increment a gauge by amount.

        Args:
            name: Gauge name
            amount: Amount to increment by
        """
        with self._lock:
            if name in self._gauges:
                self._gauges[name].inc(amount)
            else:
                logger.warning(f"Gauge {name} not registered")

    def dec_gauge(self, name: str, amount: float = 1.0) -> None:
        """
        Decrement a gauge by amount.

        Args:
            name: Gauge name
            amount: Amount to decrement by
        """
        with self._lock:
            if name in self._gauges:
                self._gauges[name].dec(amount)
            else:
                logger.warning(f"Gauge {name} not registered")

    def observe_histogram(self, name: str, value: float) -> None:
        """
        Record an observation in a histogram.

        Args:
            name: Histogram name
            value: Value to observe
        """
        with self._lock:
            if name in self._histograms:
                self._histograms[name].observe(value)
            else:
                logger.warning(f"Histogram {name} not registered")

    def get_uptime(self) -> float:
        """
        Get collector uptime in seconds.

        Returns:
            Uptime in seconds
        """
        return time.time() - self._start_time

    def update_daemon_metrics(self, pid: int | None = None) -> None:
        """
        Update daemon health metrics (uptime, memory, CPU).

        Args:
            pid: Process ID to monitor. If None, uses current process.
        """
        import os

        import psutil

        try:
            # Get process
            process = psutil.Process(pid) if pid else psutil.Process(os.getpid())

            # Update uptime
            self.set_gauge("daemon_uptime_seconds", self.get_uptime())

            # Update memory usage
            mem_info = process.memory_info()
            self.set_gauge("daemon_memory_usage_bytes", float(mem_info.rss))

            # Update CPU usage
            cpu_percent = process.cpu_percent(interval=0.1)
            self.set_gauge("daemon_cpu_percent", cpu_percent)

        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.warning(f"Failed to update daemon metrics: {e}")

    def record_mcp_call(self, duration: float, success: bool = True) -> None:
        """
        Record an MCP call with duration and success status.

        Args:
            duration: Call duration in seconds
            success: Whether the call succeeded
        """
        self.inc_counter("mcp_calls_total")
        if success:
            self.inc_counter("mcp_calls_succeeded_total")
        else:
            self.inc_counter("mcp_calls_failed_total")
        self.observe_histogram("mcp_call_duration_seconds", duration)

    def record_http_request(self, duration: float, error: bool = False) -> None:
        """
        Record an HTTP request with duration and error status.

        Args:
            duration: Request duration in seconds
            error: Whether the request resulted in an error
        """
        self.inc_counter("http_requests_total")
        if error:
            self.inc_counter("http_requests_errors_total")
        self.observe_histogram("http_request_duration_seconds", duration)

    def record_memory_save(self, duration: float, success: bool = True) -> None:
        """
        Record a memory save operation.

        Args:
            duration: Operation duration in seconds
            success: Whether the save succeeded
        """
        self.inc_counter("memory_saves_total")
        if success:
            self.inc_counter("memory_saves_succeeded_total")
        else:
            self.inc_counter("memory_saves_failed_total")
        self.observe_histogram("memory_save_duration_seconds", duration)

    def record_context_restore(self, duration: float, success: bool = True) -> None:
        """
        Record a context restore operation.

        Args:
            duration: Operation duration in seconds
            success: Whether the restore succeeded
        """
        self.inc_counter("context_restores_total")
        if success:
            self.inc_counter("context_restores_succeeded_total")
        else:
            self.inc_counter("context_restores_failed_total")
        self.observe_histogram("context_restore_duration_seconds", duration)

    def get_all_metrics(self) -> dict[str, Any]:
        """
        Get all metrics as a dictionary.

        Returns:
            Dictionary containing all metrics
        """
        with self._lock:
            return {
                "counters": {
                    name: {"value": counter.value, "labels": counter.labels}
                    for name, counter in self._counters.items()
                },
                "gauges": {
                    name: {"value": gauge.value, "labels": gauge.labels}
                    for name, gauge in self._gauges.items()
                },
                "histograms": {
                    name: {
                        "count": hist.count,
                        "sum": hist.sum,
                        "buckets": hist.bucket_counts,
                        "labels": hist.labels,
                    }
                    for name, hist in self._histograms.items()
                },
                "uptime_seconds": self.get_uptime(),
            }

    def export_prometheus(self) -> str:
        """
        Export metrics in Prometheus text format.

        Returns:
            Metrics in Prometheus exposition format
        """
        lines = []

        with self._lock:
            # Export counters
            for name, counter in self._counters.items():
                lines.append(f"# HELP {name} {counter.help_text}")
                lines.append(f"# TYPE {name} counter")
                labels_str = self._format_labels(counter.labels)
                lines.append(f"{name}{labels_str} {counter.value}")

            # Export gauges
            for name, gauge in self._gauges.items():
                lines.append(f"# HELP {name} {gauge.help_text}")
                lines.append(f"# TYPE {name} gauge")
                labels_str = self._format_labels(gauge.labels)
                lines.append(f"{name}{labels_str} {gauge.value}")

            # Export histograms
            for name, hist in self._histograms.items():
                lines.append(f"# HELP {name} {hist.help_text}")
                lines.append(f"# TYPE {name} histogram")
                labels_str = self._format_labels(hist.labels)

                # Export bucket counts
                for bucket, count in sorted(hist.bucket_counts.items()):
                    bucket_labels = {**hist.labels, "le": str(bucket)}
                    bucket_labels_str = self._format_labels(bucket_labels)
                    lines.append(f"{name}_bucket{bucket_labels_str} {count}")

                # Export +Inf bucket
                inf_labels = {**hist.labels, "le": "+Inf"}
                inf_labels_str = self._format_labels(inf_labels)
                lines.append(f"{name}_bucket{inf_labels_str} {hist.count}")

                # Export sum and count
                lines.append(f"{name}_sum{labels_str} {hist.sum}")
                lines.append(f"{name}_count{labels_str} {hist.count}")

        return "\n".join(lines) + "\n"

    def _format_labels(self, labels: dict[str, str]) -> str:
        """
        Format labels for Prometheus exposition format.

        Args:
            labels: Label dictionary

        Returns:
            Formatted labels string (e.g., '{method="GET",status="200"}')
        """
        if not labels:
            return ""

        label_pairs = [f'{k}="{v}"' for k, v in sorted(labels.items())]
        return "{" + ",".join(label_pairs) + "}"


# Global metrics collector instance
_metrics_collector: MetricsCollector | None = None


def get_metrics_collector() -> MetricsCollector:
    """
    Get global metrics collector instance.

    Returns:
        MetricsCollector instance
    """
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector
