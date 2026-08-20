"""
Tests for telemetry providers.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import generate_latest

import gobby.telemetry.providers as providers
from gobby.telemetry.config import TelemetrySettings
from gobby.telemetry.providers import (
    get_meter_provider,
    get_tracer_provider,
    shutdown_providers,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def cleanup_providers() -> Iterator[None]:
    """Ensure providers are cleared after each test."""
    shutdown_providers()
    yield
    shutdown_providers()


def test_get_tracer_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test TracerProvider creation and caching."""
    monkeypatch.setattr(
        providers,
        "create_metric_readers",
        MagicMock(side_effect=AssertionError("tracer provider created metric readers")),
    )
    config = TelemetrySettings(service_name="test-trace", traces_enabled=True)
    provider1 = get_tracer_provider(config)
    assert isinstance(provider1, TracerProvider)
    assert provider1.resource.attributes["service.name"] == "test-trace"

    provider2 = get_tracer_provider(config)
    assert provider1 is provider2


def test_add_span_storage_exporter_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = MagicMock()
    batch_processors: list[object] = []

    def fake_batch_processor(exporter: object) -> object:
        processor = MagicMock(exporter=exporter)
        batch_processors.append(processor)
        return processor

    monkeypatch.setattr(providers, "_TRACER_PROVIDER", provider)
    monkeypatch.setattr(providers, "_SPAN_STORAGE_PROCESSOR", None)
    monkeypatch.setattr(providers, "BatchSpanProcessor", fake_batch_processor)

    providers.add_span_storage_exporter(MagicMock(), broadcast_callback=MagicMock())
    providers.add_span_storage_exporter(MagicMock(), broadcast_callback=MagicMock())

    assert provider.add_span_processor.call_count == 1
    assert len(batch_processors) == 1

    providers.shutdown_providers()

    batch_processors[0].force_flush.assert_called_once()
    batch_processors[0].shutdown.assert_called_once()
    provider.shutdown.assert_not_called()
    assert providers._SPAN_STORAGE_PROCESSOR is None

    monkeypatch.setattr(providers, "_TRACER_PROVIDER", provider)
    providers.add_span_storage_exporter(MagicMock())
    assert provider.add_span_processor.call_count == 2
    assert len(batch_processors) == 2


def test_get_meter_provider(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test MeterProvider creation and caching."""
    monkeypatch.setattr(
        providers,
        "create_span_exporters",
        MagicMock(side_effect=AssertionError("meter provider created span exporters")),
    )
    config = TelemetrySettings(service_name="test-metrics", metrics_enabled=True)
    provider1 = get_meter_provider(config)
    assert isinstance(provider1, MeterProvider)
    # MeterProvider internal attribute access
    assert provider1._sdk_config.resource.attributes["service.name"] == "test-metrics"

    provider2 = get_meter_provider(config)
    assert provider1 is provider2

    caplog.set_level("WARNING")
    generate_latest()
    assert "Cannot call collect on a MetricReader" not in caplog.text


def test_provider_acquisition_reuses_api_global_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer_provider = TracerProvider()
    meter_provider = MeterProvider()
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: tracer_provider)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: meter_provider)

    config = TelemetrySettings()

    assert get_tracer_provider(config) is tracer_provider
    assert get_meter_provider(config) is meter_provider


def test_shutdown_providers_clears_without_stopping_interpreter_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer_provider = TracerProvider()
    meter_provider = MeterProvider()
    tracer_shutdown = MagicMock()
    meter_shutdown = MagicMock()
    monkeypatch.setattr(tracer_provider, "shutdown", tracer_shutdown)
    monkeypatch.setattr(meter_provider, "shutdown", meter_shutdown)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: tracer_provider)
    monkeypatch.setattr(metrics, "get_meter_provider", lambda: meter_provider)
    get_tracer_provider(TelemetrySettings())
    get_meter_provider(TelemetrySettings())

    shutdown_providers()

    tracer_shutdown.assert_not_called()
    meter_shutdown.assert_not_called()
    assert providers._TRACER_PROVIDER is None
    assert providers._METER_PROVIDER is None


def test_shutdown_providers_flushes_and_stops_owned_providers() -> None:
    tracer_provider = MagicMock()
    meter_provider = MagicMock()
    providers._TRACER_PROVIDER = tracer_provider
    providers._METER_PROVIDER = meter_provider
    providers._OWNED_TRACER_PROVIDER = tracer_provider
    providers._OWNED_METER_PROVIDER = meter_provider

    shutdown_providers()

    tracer_provider.force_flush.assert_called_once_with()
    tracer_provider.shutdown.assert_called_once_with()
    meter_provider.force_flush.assert_called_once_with()
    meter_provider.shutdown.assert_called_once_with()
    assert providers._TRACER_PROVIDER is None
    assert providers._METER_PROVIDER is None
    assert providers._OWNED_TRACER_PROVIDER is None
    assert providers._OWNED_METER_PROVIDER is None


def test_shutdown_providers_logs_processor_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    processor = MagicMock()
    processor.shutdown.side_effect = RuntimeError("processor shutdown failed")
    providers._SPAN_STORAGE_PROCESSOR = processor

    with caplog.at_level("ERROR", logger="gobby.telemetry.providers"):
        shutdown_providers()

    processor.shutdown.assert_called_once_with()
    assert "Failed to shut down span storage processor telemetry provider" in caplog.text
