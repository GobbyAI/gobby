"""
Tests for TelemetryMetrics instruments.
"""

import importlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol, cast
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    InMemoryMetricReader,
    NumberDataPoint,
)

from gobby.telemetry import instruments
from gobby.telemetry.instruments import TelemetryMetrics


class _GaugePoint(Protocol):
    value: float


@pytest.fixture
def meter_provider() -> tuple[MeterProvider, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return provider, reader


@pytest.fixture
def metrics_collector(
    meter_provider: tuple[MeterProvider, InMemoryMetricReader],
) -> TelemetryMetrics:
    provider, _ = meter_provider
    meter = provider.get_meter("test")
    return TelemetryMetrics(meter)


def test_get_telemetry_metrics_creates_one_instance_across_threads() -> None:
    thread_count = 8
    start = threading.Barrier(thread_count)
    constructor_delay = threading.Event()
    instance = MagicMock(spec=TelemetryMetrics)

    def get_metrics() -> TelemetryMetrics:
        start.wait()
        return instruments.get_telemetry_metrics()

    def construct_metrics(_meter: object) -> MagicMock:
        constructor_delay.wait(timeout=0.05)
        return instance

    with (
        patch.object(instruments, "_telemetry_metrics", None),
        patch.object(instruments, "TelemetryMetrics", side_effect=construct_metrics) as constructor,
        ThreadPoolExecutor(max_workers=thread_count) as executor,
    ):
        results = list(executor.map(lambda _: get_metrics(), range(thread_count)))

    assert constructor.call_count == 1
    assert all(result is instance for result in results)


def test_inc_counter(
    metrics_collector: TelemetryMetrics, meter_provider: tuple[MeterProvider, InMemoryMetricReader]
) -> None:
    _, reader = meter_provider
    metrics_collector.inc_counter("http_requests_total", amount=2)

    # Check OTel
    data = reader.get_metrics_data()
    assert data is not None
    found = False
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "http_requests_total":
                    found = True
                    point = metric.data.data_points[0]
                    assert isinstance(point, NumberDataPoint)
                    assert point.value == 2
    assert found

    # Check get_all_metrics
    all_metrics = metrics_collector.get_all_metrics()
    assert all_metrics["counters"]["http_requests_total"]["value"] == 2


def test_autonomous_stuck_lifecycle_counter_registered(metrics_collector: TelemetryMetrics) -> None:
    metrics_collector.inc_counter("agent_lifecycle_autonomous_stuck_detected_total")

    all_metrics = metrics_collector.get_all_metrics()
    assert all_metrics["counters"]["agent_lifecycle_autonomous_stuck_detected_total"]["value"] == 1


def test_hook_phase_duration_histogram_is_not_registered(
    metrics_collector: TelemetryMetrics,
) -> None:
    """#23289 removed the unowned hook phase-timing metric."""
    histograms = metrics_collector.get_all_metrics()["histograms"]

    assert "hook_phase_duration_seconds" not in histograms


def test_hook_phase_timing_module_is_gone() -> None:
    """#23289 removed the hook phase-timing API that fed the metric."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("gobby.hooks.phase_timing")


def test_set_gauge(
    metrics_collector: TelemetryMetrics, meter_provider: tuple[MeterProvider, InMemoryMetricReader]
) -> None:
    _, reader = meter_provider
    metrics_collector.set_gauge("mcp_active_connections", value=5.0)

    # Check OTel
    data = reader.get_metrics_data()
    assert data is not None
    found = False
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "mcp_active_connections":
                    found = True
                    point = metric.data.data_points[0]
                    assert isinstance(point, NumberDataPoint)
                    assert point.value == 5.0
    assert found

    # Check get_all_metrics
    all_metrics = metrics_collector.get_all_metrics()
    assert all_metrics["gauges"]["mcp_active_connections"]["value"] == 5.0


def test_inc_dec_gauge(metrics_collector: TelemetryMetrics) -> None:
    metrics_collector.inc_gauge("mcp_active_connections", amount=2.0)
    assert metrics_collector.get_all_metrics()["gauges"]["mcp_active_connections"]["value"] == 2.0

    metrics_collector.dec_gauge("mcp_active_connections", amount=1.0)
    assert metrics_collector.get_all_metrics()["gauges"]["mcp_active_connections"]["value"] == 1.0


def test_observe_histogram(
    metrics_collector: TelemetryMetrics, meter_provider: tuple[MeterProvider, InMemoryMetricReader]
) -> None:
    _, reader = meter_provider
    metrics_collector.observe_histogram("http_request_duration_seconds", value=0.5)

    # Check OTel
    data = reader.get_metrics_data()
    assert data is not None
    found = False
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "http_request_duration_seconds":
                    found = True
                    point = metric.data.data_points[0]
                    assert isinstance(point, HistogramDataPoint)
                    assert point.count == 1
                    assert point.sum == 0.5
    assert found

    # Check get_all_metrics
    all_metrics = metrics_collector.get_all_metrics()
    assert all_metrics["histograms"]["http_request_duration_seconds"]["count"] == 1
    assert all_metrics["histograms"]["http_request_duration_seconds"]["sum"] == 0.5


def test_consecutive_daemon_cpu_samples_measure_work(metrics_collector: TelemetryMetrics) -> None:
    """Two samples of one daemon process report the CPU used between them."""
    metrics_collector.update_daemon_metrics()
    deadline = time.perf_counter() + 0.2
    spins = 0
    while time.perf_counter() < deadline:
        spins += 1
    metrics_collector.update_daemon_metrics()
    measured = metrics_collector.get_all_metrics()["gauges"]["daemon_cpu_percent"]["value"]
    assert spins > 0
    assert measured > 0


def test_update_daemon_metrics(metrics_collector: TelemetryMetrics) -> None:
    with patch("psutil.Process") as mock_process:
        mock_p = MagicMock()
        mock_p.memory_info.return_value.rss = 1024 * 1024 * 50  # 50MB
        mock_p.cpu_percent.return_value = 5.5
        mock_process.return_value = mock_p

        metrics_collector.update_daemon_metrics()

        all_metrics = metrics_collector.get_all_metrics()
        assert all_metrics["gauges"]["daemon_memory_usage_bytes"]["value"] == 1024 * 1024 * 50
        assert all_metrics["gauges"]["daemon_cpu_percent"]["value"] == 5.5
        assert all_metrics["gauges"]["daemon_uptime_seconds"]["value"] >= 0


def test_concurrent_daemon_cpu_samples_do_not_overlap(metrics_collector: TelemetryMetrics) -> None:
    first_sample_started = threading.Event()
    second_call_started = threading.Event()
    release_first_sample = threading.Event()
    overlapping_sample = threading.Event()
    sample_lock = threading.Lock()
    active_samples = 0

    def sample_cpu_percent(*, interval: float | None = None) -> float:
        nonlocal active_samples
        assert interval is None
        with sample_lock:
            active_samples += 1
            if active_samples > 1:
                overlapping_sample.set()
        first_sample_started.set()
        try:
            if not release_first_sample.wait(timeout=5):
                raise TimeoutError("first CPU sample was not released")
            return 5.5
        finally:
            with sample_lock:
                active_samples -= 1

    def second_call() -> None:
        second_call_started.set()
        metrics_collector.update_daemon_metrics()

    with patch("psutil.Process") as mock_process:
        mock_p = mock_process.return_value
        mock_p.memory_info.return_value.rss = 1024
        mock_p.cpu_percent.side_effect = sample_cpu_percent

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(metrics_collector.update_daemon_metrics)
            try:
                assert first_sample_started.wait(timeout=5)
                second = executor.submit(second_call)
                assert second_call_started.wait(timeout=5)
                assert not overlapping_sample.wait(timeout=0.2)
            finally:
                release_first_sample.set()

            first.result(timeout=5)
            second.result(timeout=5)

        assert mock_p.cpu_percent.call_count == 2
        assert not overlapping_sample.is_set()


def test_observable_gauge_callback(
    metrics_collector: TelemetryMetrics,
    meter_provider: tuple[MeterProvider, InMemoryMetricReader],
) -> None:
    _, reader = meter_provider
    metrics_collector.set_gauge("daemon_uptime_seconds", value=123.45)

    # OTel ObservableGauge will call the callback during collect
    data = reader.get_metrics_data()
    assert data is not None
    found = False
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "daemon_uptime_seconds":
                    found = True
                    point = cast(_GaugePoint, metric.data.data_points[0])
                    assert point.value == 123.45
    assert found


def test_seconds_histograms_resolve_sub_second_waits(
    metrics_collector: TelemetryMetrics,
    meter_provider: tuple[MeterProvider, InMemoryMetricReader],
) -> None:
    # The SDK default boundaries (0, 5, 10, ... 10000) are millisecond-scale; with them
    # every sub-5 s pool acquire lands in one bucket and p95 queries are meaningless.
    _, reader = meter_provider
    metrics_collector.observe_histogram("database_pool_acquire_wait_seconds", value=0.3)

    data = reader.get_metrics_data()
    assert data is not None
    points = [
        point
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == "database_pool_acquire_wait_seconds"
        for point in metric.data.data_points
        if isinstance(point, HistogramDataPoint)
    ]
    assert len(points) == 1
    bounds = list(points[0].explicit_bounds)
    assert bounds == list(instruments.SECONDS_HISTOGRAM_BOUNDARIES)
    assert bounds[0] < 0.3 < bounds[-1]
    assert points[0].bucket_counts[bounds.index(0.5)] == 1
