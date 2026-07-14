"""
Tests for telemetry exporters factory.
"""

from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from gobby.telemetry.config import TelemetrySettings
from gobby.telemetry.exporters import create_metric_readers, create_span_exporters


def test_create_span_exporters_defaults() -> None:
    """Test create_span_exporters with default settings."""
    assert create_span_exporters(TelemetrySettings()) == []


def test_create_span_exporters_traces_enabled() -> None:
    """Test create_span_exporters with traces enabled."""
    config = TelemetrySettings(
        traces_enabled=True,
        traces_to_console=True,
        exporter={"otlp_endpoint": "http://localhost:4317"},
    )
    span_exporters = create_span_exporters(config)

    assert len(span_exporters) == 2
    assert any(isinstance(e, ConsoleSpanExporter) for e in span_exporters)
    assert any(isinstance(e, OTLPSpanExporter) for e in span_exporters)


def test_create_metric_readers_defaults() -> None:
    """Test create_metric_readers with default settings."""
    metric_readers = create_metric_readers(TelemetrySettings())

    assert len(metric_readers) == 1
    assert isinstance(metric_readers[0], PrometheusMetricReader)
    metric_readers[0].shutdown()


def test_create_metric_readers_metrics_disabled() -> None:
    """Test create_metric_readers with metrics disabled."""
    config = TelemetrySettings(metrics_enabled=False)
    metric_readers = create_metric_readers(config)
    assert len(metric_readers) == 0


def test_create_metric_readers_prometheus_disabled() -> None:
    """Test create_metric_readers with prometheus disabled."""
    config = TelemetrySettings(metrics_enabled=True, exporter={"prometheus_enabled": False})
    metric_readers = create_metric_readers(config)
    assert len(metric_readers) == 0
