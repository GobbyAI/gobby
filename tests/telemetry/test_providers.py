"""
Tests for telemetry providers.
"""

from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import generate_latest

import gobby.telemetry.providers as providers
from gobby.telemetry.config import TelemetrySettings
from gobby.telemetry.providers import (
    get_logger_provider,
    get_meter_provider,
    get_tracer_provider,
    shutdown_providers,
)


@pytest.fixture(autouse=True)
def cleanup_providers() -> None:
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
        batch_processors.append(exporter)
        return exporter

    monkeypatch.setattr(providers, "_TRACER_PROVIDER", provider)
    monkeypatch.setattr(providers, "_SPAN_STORAGE_EXPORTER_REGISTERED", False)
    monkeypatch.setattr(providers, "BatchSpanProcessor", fake_batch_processor)

    providers.add_span_storage_exporter(MagicMock(), broadcast_callback=MagicMock())
    providers.add_span_storage_exporter(MagicMock(), broadcast_callback=MagicMock())

    assert provider.add_span_processor.call_count == 1
    assert len(batch_processors) == 1

    providers.shutdown_providers()

    provider.shutdown.assert_called_once()
    assert providers._SPAN_STORAGE_EXPORTER_REGISTERED is False


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


def test_get_logger_provider() -> None:
    """Test LoggerProvider creation and caching."""
    config = TelemetrySettings(service_name="test-logs")
    provider1 = get_logger_provider(config)
    assert isinstance(provider1, LoggerProvider)
    assert provider1.resource.attributes["service.name"] == "test-logs"

    provider2 = get_logger_provider(config)
    assert provider1 is provider2


def test_shutdown_providers() -> None:
    """Test shutdown of all providers."""
    config = TelemetrySettings()
    p_trace = get_tracer_provider(config)
    p_meter = get_meter_provider(config)
    p_logger = get_logger_provider(config)

    shutdown_providers()

    # Getting them again should create new instances
    assert get_tracer_provider(config) is not p_trace
    assert get_meter_provider(config) is not p_meter
    assert get_logger_provider(config) is not p_logger


def test_shutdown_providers_logs_and_continues_after_provider_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracer_provider = MagicMock()
    meter_provider = MagicMock()
    logger_provider = MagicMock()
    tracer_provider.shutdown.side_effect = RuntimeError("tracer shutdown failed")
    providers._TRACER_PROVIDER = tracer_provider
    providers._METER_PROVIDER = meter_provider
    providers._LOGGER_PROVIDER = logger_provider

    with caplog.at_level("ERROR", logger="gobby.telemetry.providers"):
        shutdown_providers()

    tracer_provider.shutdown.assert_called_once_with()
    meter_provider.shutdown.assert_called_once_with()
    logger_provider.shutdown.assert_called_once_with()
    assert "Failed to shut down tracer telemetry provider" in caplog.text
