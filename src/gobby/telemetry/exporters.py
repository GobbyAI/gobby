"""
OpenTelemetry exporter factories.

Creates configured exporters independently for traces and metrics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# opentelemetry-api and sdk are in dependencies
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter as OTLPGRPCSpanExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as OTLPHTTPSpanExporter,
)
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics.export import MetricReader
    from opentelemetry.sdk.trace.export import SpanExporter

    from gobby.telemetry.config import TelemetrySettings


def create_span_exporters(config: TelemetrySettings) -> list[SpanExporter]:
    """Create configured span exporters."""
    span_exporters: list[SpanExporter] = []

    if config.traces_enabled:
        if config.traces_to_console:
            span_exporters.append(ConsoleSpanExporter())

        if config.exporter.otlp_endpoint:
            raw_headers = config.exporter.otlp_headers
            headers: dict[str, str] | None = (
                {str(k): str(v) for k, v in raw_headers.items()} if raw_headers else None
            )
            if config.exporter.otlp_protocol == "http":
                span_exporters.append(
                    OTLPHTTPSpanExporter(
                        endpoint=config.exporter.otlp_endpoint,
                        headers=headers,
                    )
                )
            else:
                span_exporters.append(
                    OTLPGRPCSpanExporter(
                        endpoint=config.exporter.otlp_endpoint,
                        headers=headers,
                    )
                )

    return span_exporters


def create_metric_readers(config: TelemetrySettings) -> list[MetricReader]:
    """Create configured metric readers."""
    metric_readers: list[MetricReader] = []

    if config.metrics_enabled:
        if config.exporter.prometheus_enabled:
            metric_readers.append(PrometheusMetricReader())

    return metric_readers
