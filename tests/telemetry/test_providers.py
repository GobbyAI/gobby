"""
Tests for telemetry providers.
"""

from unittest.mock import MagicMock

import pytest
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


@pytest.fixture(autouse=True)
def cleanup_providers() -> None:
    """Ensure providers are cleared after each test."""
    shutdown_providers()
    providers._TRACER_PROVIDER = None
    providers._METER_PROVIDER = None
    yield
    shutdown_providers()
    for provider in (providers._TRACER_PROVIDER, providers._METER_PROVIDER):
        if provider is not None:
            provider.shutdown()
    providers._TRACER_PROVIDER = None
    providers._METER_PROVIDER = None


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

    batch_processors[0].shutdown.assert_called_once()
    provider.shutdown.assert_not_called()
    assert providers._SPAN_STORAGE_PROCESSOR is None

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

    generate_latest()
    assert "Cannot call collect on a MetricReader" not in caplog.text


def test_provider_acquisition_reuses_api_global_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracer_provider = TracerProvider()
    meter_provider = MeterProvider()
    monkeypatch.setattr(providers.trace, "get_tracer_provider", lambda: tracer_provider)
    monkeypatch.setattr(providers.metrics, "get_meter_provider", lambda: meter_provider)

    config = TelemetrySettings()

    assert get_tracer_provider(config) is tracer_provider
    assert get_meter_provider(config) is meter_provider


def test_shutdown_providers_preserves_interpreter_providers() -> None:
    """Lifecycle shutdown preserves interpreter-latched providers."""
    config = TelemetrySettings()
    p_trace = get_tracer_provider(config)
    p_meter = get_meter_provider(config)

    shutdown_providers()

    assert get_tracer_provider(config) is p_trace
    assert get_meter_provider(config) is p_meter


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
