"""
Tests for telemetry configuration.
"""

import pytest
from pydantic import ValidationError

from gobby.telemetry.config import TelemetrySettings


def test_telemetry_settings_defaults() -> None:
    """Test default values of TelemetrySettings."""
    settings = TelemetrySettings()
    assert settings.service_name == "gobby-daemon"
    assert settings.log_level == "info"
    assert settings.log_format == "text"
    assert settings.log_file == "~/.gobby/logs/gobby.log"
    assert settings.traces_enabled is True
    assert settings.traces_to_console is False
    assert settings.trace_sample_rate == 1.0
    assert settings.metrics_enabled is True
    assert settings.exporter.prometheus_enabled is True
    assert settings.exporter.otlp_endpoint is None
    assert settings.llm_tracing.providers == ["anthropic", "openai"]


def test_telemetry_settings_validation_positive() -> None:
    """Test positive values validation for max_size_mb and backup_count."""
    with pytest.raises(ValidationError):
        TelemetrySettings(max_size_mb=0)
    with pytest.raises(ValidationError):
        TelemetrySettings(backup_count=0)
    with pytest.raises(ValidationError):
        TelemetrySettings(max_size_mb=-1)


def test_telemetry_settings_trace_sample_rate_validation() -> None:
    """Test trace_sample_rate validation (0.0 to 1.0)."""
    assert TelemetrySettings(trace_sample_rate=0.0).trace_sample_rate == 0.0
    assert TelemetrySettings(trace_sample_rate=0.5).trace_sample_rate == 0.5
    assert TelemetrySettings(trace_sample_rate=1.0).trace_sample_rate == 1.0

    with pytest.raises(ValidationError):
        TelemetrySettings(trace_sample_rate=-0.1)
    with pytest.raises(ValidationError):
        TelemetrySettings(trace_sample_rate=1.1)


def test_telemetry_settings_serialization() -> None:
    """Test serialization of TelemetrySettings."""
    settings = TelemetrySettings(service_name="test-service", traces_enabled=True)
    dump = settings.model_dump()
    assert dump["service_name"] == "test-service"
    assert dump["traces_enabled"] is True
    assert dump["exporter"]["prometheus_enabled"] is True
