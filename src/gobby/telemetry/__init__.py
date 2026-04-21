"""
Gobby Telemetry Module.

Provides public API for OpenTelemetry tracing, metrics, and logging integration.
Replaces legacy utils/logging.py and utils/metrics.py modules.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace
from opentelemetry.instrumentation.logging import LoggingInstrumentor

from gobby.telemetry.context import extract_from_env, inject_into_env
from gobby.telemetry.instruments import (
    dec_gauge,
    get_telemetry_metrics,
    inc_counter,
    inc_gauge,
    observe_histogram,
    set_gauge,
)
from gobby.telemetry.providers import (
    get_logger_provider,
    get_meter_provider,
    get_tracer_provider,
    shutdown_providers,
)
from gobby.telemetry.tracing import (
    add_span_attributes,
    create_span,
    current_span,
    record_exception,
    traced,
)

__all__ = [
    "init_telemetry",
    "get_tracer",
    "get_meter",
    "shutdown_telemetry",
    "extract_from_env",
    "inject_into_env",
    "add_span_attributes",
    "create_span",
    "current_span",
    "record_exception",
    "traced",
    "inc_counter",
    "inc_gauge",
    "dec_gauge",
    "set_gauge",
    "observe_histogram",
    "get_telemetry_metrics",
]

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter
    from opentelemetry.trace import Tracer

    from gobby.telemetry.config import TelemetrySettings


def init_telemetry(config: TelemetrySettings) -> None:
    """
    Initialize telemetry system with given settings.

    Args:
        config: TelemetrySettings instance.
    """
    # 0. LLM instrumentors (must run before LLM client instantiation)
    if config.llm_tracing.enabled:
        try:
            from gobby.telemetry.instrumentors import setup_llm_instrumentors

            setup_llm_instrumentors(
                capture_content=config.llm_tracing.capture_content,
                providers=config.llm_tracing.providers,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to setup LLM instrumentors — tracing will be incomplete",
                exc_info=True,
            )

    # 1. Tracing
    tracer_provider = get_tracer_provider(config)
    trace.set_tracer_provider(tracer_provider)

    # 2. Metrics
    meter_provider = get_meter_provider(config)
    metrics.set_meter_provider(meter_provider)

    # 3. Logging Bridge
    # Configure OTel logging provider and handler bridge
    get_logger_provider(config)
    # LoggingInstrumentor handles bridge to root logger
    LoggingInstrumentor().instrument(set_logging_format=(config.log_format == "text"))


def get_tracer(name: str, version: str | None = None) -> Tracer:
    """
    Get a tracer instance.

    Args:
        name: Name of the tracer (usually __name__).
        version: Optional version of the tracer.

    Returns:
        Tracer instance.
    """
    return trace.get_tracer(name, version or "")


def get_meter(name: str, version: str | None = None) -> Meter:
    """
    Get a meter instance.

    Args:
        name: Name of the meter.
        version: Optional version of the meter.

    Returns:
        Meter instance.
    """
    return metrics.get_meter(name, version or "")


def shutdown_telemetry() -> None:
    """
    Shutdown telemetry and clear cache.
    """
    # Uninstrument logging bridge
    instrumentor = LoggingInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()

    # Shutdown OTel providers
    shutdown_providers()
