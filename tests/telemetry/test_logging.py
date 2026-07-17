import io
import json
import logging
import warnings
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider

from gobby.config.logging import (
    ERROR_LOG_FILENAME,
    HOOK_MANAGER_LOG_FILENAME,
    MAIN_LOG_FILENAME,
    MCP_CLIENT_LOG_FILENAME,
    MCP_SERVER_LOG_FILENAME,
    LoggingSettings,
    resolved_log_path,
)
from gobby.telemetry import init_telemetry, shutdown_telemetry
from gobby.telemetry.config import TelemetrySettings
from gobby.telemetry.logging import (
    JsonOTelFormatter,
    OTelTraceFormatter,
    setup_file_logging,
    setup_otel_logging,
)


@pytest.fixture
def temp_log_dir(tmp_path: Path) -> Path:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return log_dir


@pytest.fixture
def telemetry_config() -> TelemetrySettings:
    return TelemetrySettings()


@pytest.fixture
def logging_config(temp_log_dir: Path) -> LoggingSettings:
    return LoggingSettings(dir=str(temp_log_dir), level="debug", format="text")


def test_otel_trace_formatter_injects_trace_id():
    formatter = OTelTraceFormatter("%(trace_id)s - %(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="test message",
        args=(),
        exc_info=None,
    )

    # Without active span
    assert " - test message" in formatter.format(record)
    assert record.trace_id == "-"

    # With active span
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("test-span") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        formatted = formatter.format(record)
        assert trace_id in formatted
        assert record.trace_id == trace_id


def test_json_otel_formatter_produces_json():
    formatter = JsonOTelFormatter()
    record = logging.LogRecord(
        name="gobby.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="test message",
        args=(),
        exc_info=None,
    )

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "INFO"
    assert data["message"] == "test message"
    assert data["name"] == "gobby.test"


def test_json_otel_formatter_serializes_non_json_extra_values():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonOTelFormatter())
    logger = logging.getLogger("gobby.test.json.extra")
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        path = Path("/x")
        created_at = datetime(2026, 7, 14, 12, 30)
        logger.info("structured message", extra={"path": path, "created_at": created_at})
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    data = json.loads(stream.getvalue())
    assert data["message"] == "structured message"
    assert data["path"] == str(path)
    assert data["created_at"] == str(created_at)


def test_setup_otel_logging_creates_files(telemetry_config, logging_config):
    setup_otel_logging(telemetry_config, logging_config)

    # Check that handlers are attached to root logger
    root_logger = logging.getLogger("gobby")
    assert len(root_logger.handlers) >= 3  # main, error, otel

    # Trigger some logs
    root_logger.info("Main log message")
    root_logger.error("Error log message")

    logging.getLogger("gobby.hooks").info("Hook message")
    logging.getLogger("gobby.mcp.server").info("MCP server message")
    logging.getLogger("gobby.mcp.client").info("MCP client message")
    # Verify files exist
    assert resolved_log_path(logging_config, MAIN_LOG_FILENAME).exists()
    assert resolved_log_path(logging_config, ERROR_LOG_FILENAME).exists()
    assert resolved_log_path(logging_config, HOOK_MANAGER_LOG_FILENAME).exists()
    assert resolved_log_path(logging_config, MCP_SERVER_LOG_FILENAME).exists()
    assert resolved_log_path(logging_config, MCP_CLIENT_LOG_FILENAME).exists()

    # Verify content
    content = resolved_log_path(logging_config, MAIN_LOG_FILENAME).read_text()
    assert "Main log message" in content

    error_content = resolved_log_path(logging_config, ERROR_LOG_FILENAME).read_text()
    assert "Error log message" in error_content

    hook_content = resolved_log_path(logging_config, HOOK_MANAGER_LOG_FILENAME).read_text()
    assert "Hook message" in hook_content


def test_setup_otel_logging_rotation(telemetry_config, logging_config):
    # Set small max_size_mb for testing rotation
    logging_config.max_size_mb = 1  # 1MB
    logging_config.backup_count = 2

    setup_otel_logging(telemetry_config, logging_config)

    logger = logging.getLogger("gobby")
    # Write a lot of data
    large_msg = "x" * 1024 * 100  # 100KB
    for _ in range(15):  # 1.5MB total
        logger.info(large_msg)

    # Check if rotated file exists
    assert Path(f"{resolved_log_path(logging_config, MAIN_LOG_FILENAME)}.1").exists()


def test_setup_otel_logging_verbose_sets_debug(telemetry_config, logging_config):
    logging_config.level = "info"
    setup_otel_logging(telemetry_config, logging_config, verbose=True)

    root_logger = logging.getLogger("gobby")
    assert root_logger.level == logging.DEBUG


def test_setup_otel_logging_json_format(telemetry_config, logging_config):
    logging_config.format = "json"
    setup_otel_logging(telemetry_config, logging_config)

    root_logger = logging.getLogger("gobby")
    handler = [
        h for h in root_logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ][0]
    assert isinstance(handler.formatter, JsonOTelFormatter)


def test_setup_otel_logging_sub_loggers(telemetry_config, logging_config):
    setup_otel_logging(telemetry_config, logging_config)

    for name in ["gobby.hooks", "gobby.mcp.server", "gobby.mcp.client"]:
        logger = logging.getLogger(name)
        assert not logger.propagate
        assert len(logger.handlers) >= 1
        assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logger.handlers)


def test_setup_otel_logging_suppresses_websockets_info(telemetry_config, logging_config):
    logging.getLogger("websockets").setLevel(logging.INFO)
    logging.getLogger("websockets.server").setLevel(logging.INFO)

    setup_otel_logging(telemetry_config, logging_config)

    assert logging.getLogger("websockets").level == logging.WARNING
    assert logging.getLogger("websockets.server").level == logging.WARNING


def test_setup_otel_logging_attaches_otel_handler(telemetry_config, logging_config):
    from opentelemetry.sdk._logs import LoggingHandler

    setup_otel_logging(telemetry_config, logging_config)

    root_logger = logging.getLogger("gobby")
    assert any(isinstance(h, LoggingHandler) for h in root_logger.handlers)


def test_setup_file_logging_does_not_create_otel_provider(logging_config):
    from opentelemetry.sdk._logs import LoggingHandler

    with patch("gobby.telemetry.logging.get_logger_provider") as get_logger_provider:
        setup_file_logging(logging_config)

    get_logger_provider.assert_not_called()
    root_logger = logging.getLogger("gobby")
    assert not any(isinstance(h, LoggingHandler) for h in root_logger.handlers)


def test_init_telemetry_sets_providers(telemetry_config, logging_config):
    from opentelemetry import metrics, trace

    # Clear providers if possible or just check they are set
    init_telemetry(telemetry_config, logging_config)

    assert trace.get_tracer_provider() is not None
    assert metrics.get_meter_provider() is not None


def test_daemon_init_activates_llm_instrumentor(telemetry_config, logging_config):
    from gobby.runner_init.storage import init_telemetry as daemon_init_telemetry
    from gobby.telemetry.instrumentors import _instrumented

    telemetry_config.llm_tracing.enabled = True
    telemetry_config.llm_tracing.providers = ["anthropic"]

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="'asyncio.iscoroutinefunction' is deprecated.*",
            category=DeprecationWarning,
        )
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

        instrumentor = AnthropicInstrumentor()
        try:
            daemon_init_telemetry(telemetry_config, logging_config)

            assert instrumentor.is_instrumented_by_opentelemetry
        finally:
            instrumentor.uninstrument()
            _instrumented.discard("anthropic")


def test_shutdown_telemetry_skips_uninstrument_when_not_instrumented() -> None:
    instrumentor = MagicMock()
    instrumentor.is_instrumented_by_opentelemetry = False

    with (
        patch("gobby.telemetry.LoggingInstrumentor", return_value=instrumentor),
        patch("gobby.telemetry.shutdown_providers") as mock_shutdown_providers,
    ):
        shutdown_telemetry()

    instrumentor.uninstrument.assert_not_called()
    assert instrumentor.uninstrument.call_count == 0
    assert not instrumentor.uninstrument.called
    mock_shutdown_providers.assert_called_once()
    assert mock_shutdown_providers.call_count == 1
    assert mock_shutdown_providers.call_args is not None


def test_shutdown_telemetry_uninstruments_when_active() -> None:
    instrumentor = MagicMock()
    instrumentor.is_instrumented_by_opentelemetry = True

    with (
        patch("gobby.telemetry.LoggingInstrumentor", return_value=instrumentor),
        patch("gobby.telemetry.shutdown_providers") as mock_shutdown_providers,
    ):
        shutdown_telemetry()

    instrumentor.uninstrument.assert_called_once()
    assert instrumentor.uninstrument.call_count == 1
    assert instrumentor.uninstrument.call_args is not None
    mock_shutdown_providers.assert_called_once()
    assert mock_shutdown_providers.call_count == 1
    assert mock_shutdown_providers.call_args is not None


def test_setup_otel_logging_clears_old_handlers(telemetry_config, logging_config):
    root_logger = logging.getLogger("gobby")
    mock_handler = logging.NullHandler()
    root_logger.addHandler(mock_handler)
    assert mock_handler in root_logger.handlers

    setup_otel_logging(telemetry_config, logging_config)
    assert mock_handler not in root_logger.handlers


def test_otel_trace_formatter_short_name():
    formatter = OTelTraceFormatter("%(short_name)s")

    # gobby.test -> test
    record1 = logging.LogRecord("gobby.test", logging.INFO, "", 0, "msg", (), None)
    assert formatter.format(record1) == "test"

    # other.test -> other.test
    record2 = logging.LogRecord("other.test", logging.INFO, "", 0, "msg", (), None)
    assert formatter.format(record2) == "other.test"


def test_otel_trace_formatter_conditional_trace_id_append():
    """Trace ID is appended at end only when a real span is active."""
    formatter = OTelTraceFormatter("%(message)s")
    record = logging.LogRecord("test", logging.INFO, "", 0, "test message", (), None)

    # Without active span — no brackets
    formatted = formatter.format(record)
    assert formatted == "test message"
    assert "[-]" not in formatted

    # With active span — trace ID appended in brackets
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("test-span") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        formatted = formatter.format(record)
        assert formatted == f"test message [{trace_id}]"


def test_otel_trace_formatter_trace_id_before_extras():
    """Trace ID appears before extra fields when both are present."""
    formatter = OTelTraceFormatter("%(message)s")
    record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
    record.custom_field = "value"

    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span("test-span") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        formatted = formatter.format(record)
        assert f"msg [{trace_id}] | custom_field=value" == formatted


def test_otel_trace_formatter_extra_fields():
    formatter = OTelTraceFormatter("%(message)s")
    record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
    record.custom_field = "value"

    formatted = formatter.format(record)
    assert "msg | custom_field=value" in formatted
