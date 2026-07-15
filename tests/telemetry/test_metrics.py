"""
Tests for TelemetryMetrics instruments.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from gobby.telemetry import instruments
from gobby.telemetry.instruments import TelemetryMetrics


@pytest.fixture
def meter_provider():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return provider, reader


@pytest.fixture
def metrics_collector(meter_provider):
    provider, _ = meter_provider
    meter = provider.get_meter("test")
    return TelemetryMetrics(meter)


def test_get_telemetry_metrics_creates_one_instance_across_threads():
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


def test_inc_counter(metrics_collector, meter_provider):
    _, reader = meter_provider
    metrics_collector.inc_counter("http_requests_total", amount=2)

    # Check OTel
    data = reader.get_metrics_data()
    found = False
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "http_requests_total":
                    found = True
                    assert metric.data.data_points[0].value == 2
    assert found

    # Check get_all_metrics
    all_metrics = metrics_collector.get_all_metrics()
    assert all_metrics["counters"]["http_requests_total"]["value"] == 2


def test_statusline_bake_counters_registered(metrics_collector):
    all_metrics = metrics_collector.get_all_metrics()

    assert all_metrics["counters"]["statusline_posts_succeeded_total"]["value"] == 0
    assert all_metrics["counters"]["statusline_usage_gap_warnings_total"]["value"] == 0


def test_autonomous_stuck_lifecycle_counter_registered(metrics_collector):
    metrics_collector.inc_counter("agent_lifecycle_autonomous_stuck_detected_total")

    all_metrics = metrics_collector.get_all_metrics()
    assert all_metrics["counters"]["agent_lifecycle_autonomous_stuck_detected_total"]["value"] == 1


def test_set_gauge(metrics_collector, meter_provider):
    _, reader = meter_provider
    metrics_collector.set_gauge("mcp_active_connections", value=5.0)

    # Check OTel
    data = reader.get_metrics_data()
    found = False
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "mcp_active_connections":
                    found = True
                    assert metric.data.data_points[0].value == 5.0
    assert found

    # Check get_all_metrics
    all_metrics = metrics_collector.get_all_metrics()
    assert all_metrics["gauges"]["mcp_active_connections"]["value"] == 5.0


def test_inc_dec_gauge(metrics_collector):
    metrics_collector.inc_gauge("mcp_active_connections", amount=2.0)
    assert metrics_collector.get_all_metrics()["gauges"]["mcp_active_connections"]["value"] == 2.0

    metrics_collector.dec_gauge("mcp_active_connections", amount=1.0)
    assert metrics_collector.get_all_metrics()["gauges"]["mcp_active_connections"]["value"] == 1.0


def test_observe_histogram(metrics_collector, meter_provider):
    _, reader = meter_provider
    metrics_collector.observe_histogram("http_request_duration_seconds", value=0.5)

    # Check OTel
    data = reader.get_metrics_data()
    found = False
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "http_request_duration_seconds":
                    found = True
                    assert metric.data.data_points[0].count == 1
                    assert metric.data.data_points[0].sum == 0.5
    assert found

    # Check get_all_metrics
    all_metrics = metrics_collector.get_all_metrics()
    assert all_metrics["histograms"]["http_request_duration_seconds"]["count"] == 1
    assert all_metrics["histograms"]["http_request_duration_seconds"]["sum"] == 0.5


def test_update_daemon_metrics(metrics_collector):
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


def test_observable_gauge_callback(metrics_collector, meter_provider):
    _, reader = meter_provider
    metrics_collector.set_gauge("daemon_uptime_seconds", value=123.45)

    # OTel ObservableGauge will call the callback during collect
    data = reader.get_metrics_data()
    found = False
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "daemon_uptime_seconds":
                    found = True
                    assert metric.data.data_points[0].value == 123.45
    assert found
