"""
OpenTelemetry provider management.

Creates and caches TracerProvider, MeterProvider, and LoggerProvider.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from gobby.telemetry.exporters import create_exporters

if TYPE_CHECKING:
    from gobby.storage.spans import SpanStorage
    from gobby.telemetry.config import TelemetrySettings

# Globals for lazy caching
_TRACER_PROVIDER: TracerProvider | None = None
_METER_PROVIDER: MeterProvider | None = None
_LOGGER_PROVIDER: LoggerProvider | None = None
_PROVIDER_LOCK = threading.Lock()


def get_tracer_provider(config: TelemetrySettings) -> TracerProvider:
    """Get TracerProvider, creating it if needed."""
    global _TRACER_PROVIDER
    if _TRACER_PROVIDER is not None:
        return _TRACER_PROVIDER
    with _PROVIDER_LOCK:
        if _TRACER_PROVIDER is None:
            resource = Resource.create({SERVICE_NAME: config.service_name})
            sampler = ParentBased(root=TraceIdRatioBased(config.trace_sample_rate))

            span_exporters, _, _ = create_exporters(config)
            _TRACER_PROVIDER = TracerProvider(resource=resource, sampler=sampler)

            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            for exporter in span_exporters:
                _TRACER_PROVIDER.add_span_processor(BatchSpanProcessor(exporter))

    return _TRACER_PROVIDER


def add_span_storage_exporter(
    storage: SpanStorage,
    broadcast_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> None:
    """Add GobbySpanExporter to the global TracerProvider."""
    global _TRACER_PROVIDER
    if _TRACER_PROVIDER is not None:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from gobby.telemetry.span_store import GobbySpanExporter

        exporter = GobbySpanExporter(storage, broadcast_callback=broadcast_callback)
        _TRACER_PROVIDER.add_span_processor(BatchSpanProcessor(exporter))


def get_meter_provider(config: TelemetrySettings) -> MeterProvider:
    """Get MeterProvider, creating it if needed."""
    global _METER_PROVIDER
    if _METER_PROVIDER is not None:
        return _METER_PROVIDER
    with _PROVIDER_LOCK:
        if _METER_PROVIDER is None:
            resource = Resource.create({SERVICE_NAME: config.service_name})
            _, metric_readers, _ = create_exporters(config)
            _METER_PROVIDER = MeterProvider(resource=resource, metric_readers=metric_readers)

    return _METER_PROVIDER


def get_logger_provider(config: TelemetrySettings) -> LoggerProvider:
    """Get LoggerProvider, creating it if needed."""
    global _LOGGER_PROVIDER
    if _LOGGER_PROVIDER is not None:
        return _LOGGER_PROVIDER
    with _PROVIDER_LOCK:
        if _LOGGER_PROVIDER is None:
            resource = Resource.create({SERVICE_NAME: config.service_name})
            _LOGGER_PROVIDER = LoggerProvider(resource=resource)

    return _LOGGER_PROVIDER


def shutdown_providers() -> None:
    """Shutdown all providers and clear cache."""
    global _TRACER_PROVIDER, _METER_PROVIDER, _LOGGER_PROVIDER

    if _TRACER_PROVIDER is not None:
        _TRACER_PROVIDER.shutdown()
        _TRACER_PROVIDER = None

    if _METER_PROVIDER is not None:
        _METER_PROVIDER.shutdown()
        _METER_PROVIDER = None

    if _LOGGER_PROVIDER is not None:
        shutdown = cast(Callable[[], None], _LOGGER_PROVIDER.shutdown)
        shutdown()
        _LOGGER_PROVIDER = None
