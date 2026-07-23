"""
OpenTelemetry provider management.

Creates and caches TracerProvider and MeterProvider.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from gobby.telemetry.exporters import create_metric_readers, create_span_exporters

if TYPE_CHECKING:
    from gobby.storage.spans import SpanStorage
    from gobby.telemetry.config import TelemetrySettings

logger = logging.getLogger(__name__)

# Globals for lazy caching
_TRACER_PROVIDER: TracerProvider | None = None
_METER_PROVIDER: MeterProvider | None = None
_SPAN_STORAGE_PROCESSOR: BatchSpanProcessor | None = None
_PROVIDER_LOCK = threading.Lock()


def get_tracer_provider(config: TelemetrySettings) -> TracerProvider:
    """Get TracerProvider, creating it if needed."""
    global _TRACER_PROVIDER
    if _TRACER_PROVIDER is not None:
        return _TRACER_PROVIDER
    with _PROVIDER_LOCK:
        if _TRACER_PROVIDER is None:
            api_provider = trace.get_tracer_provider()
            if isinstance(api_provider, TracerProvider):
                _TRACER_PROVIDER = api_provider
            else:
                resource = Resource.create({SERVICE_NAME: config.service_name})
                sampler = ParentBased(root=TraceIdRatioBased(config.trace_sample_rate))
                span_exporters = create_span_exporters(config)
                _TRACER_PROVIDER = TracerProvider(resource=resource, sampler=sampler)

                for exporter in span_exporters:
                    _TRACER_PROVIDER.add_span_processor(BatchSpanProcessor(exporter))

    return _TRACER_PROVIDER


def add_span_storage_exporter(
    storage: SpanStorage,
    broadcast_callback: Callable[[dict[str, Any]], Any] | None = None,
) -> None:
    """Add GobbySpanExporter to the global TracerProvider."""
    global _TRACER_PROVIDER, _SPAN_STORAGE_PROCESSOR
    if _TRACER_PROVIDER is None:
        return
    with _PROVIDER_LOCK:
        if _TRACER_PROVIDER is None or _SPAN_STORAGE_PROCESSOR is not None:
            return
        from gobby.telemetry.span_store import GobbySpanExporter

        exporter = GobbySpanExporter(storage, broadcast_callback=broadcast_callback)
        processor = BatchSpanProcessor(exporter)
        _TRACER_PROVIDER.add_span_processor(processor)
        _SPAN_STORAGE_PROCESSOR = processor


def get_meter_provider(config: TelemetrySettings) -> MeterProvider:
    """Get MeterProvider, creating it if needed."""
    global _METER_PROVIDER
    if _METER_PROVIDER is not None:
        return _METER_PROVIDER
    with _PROVIDER_LOCK:
        if _METER_PROVIDER is None:
            api_provider = metrics.get_meter_provider()
            if isinstance(api_provider, MeterProvider):
                _METER_PROVIDER = api_provider
            else:
                resource = Resource.create({SERVICE_NAME: config.service_name})
                metric_readers = create_metric_readers(config)
                _METER_PROVIDER = MeterProvider(resource=resource, metric_readers=metric_readers)

    return _METER_PROVIDER


def shutdown_providers() -> None:
    """Shutdown lifecycle-owned processors without tearing down API providers."""
    global _SPAN_STORAGE_PROCESSOR

    with _PROVIDER_LOCK:
        span_storage_processor = _SPAN_STORAGE_PROCESSOR
        _SPAN_STORAGE_PROCESSOR = None

    if span_storage_processor is not None:
        _shutdown_provider("span storage processor", span_storage_processor.shutdown)


def _shutdown_provider(provider_name: str, shutdown: Callable[[], None]) -> None:
    try:
        shutdown()
    except Exception:
        logger.exception("Failed to shut down %s telemetry provider", provider_name)
