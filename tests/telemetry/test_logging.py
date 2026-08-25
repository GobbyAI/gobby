import io
import json
import logging
import warnings
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import TracerProvider

import gobby.telemetry as telemetry
from gobby.config.logging import (
    AUTOMATION_LOG_FILENAME,
    DAEMON_LOG_FILENAME,
    ERRORS_LOG_FILENAME,
    HOOKS_LOG_FILENAME,
    MCP_LOG_FILENAME,
    RULE_ALLOW_AUDIT_LOG_FILENAME,
    RUNTIME_LOG_FILENAME,
    LoggingSettings,
    resolved_log_path,
)
from gobby.telemetry import init_telemetry, shutdown_telemetry
from gobby.telemetry.config import TelemetrySettings
from gobby.telemetry.logging import (
    JsonOTelFormatter,
    OTelTraceFormatter,
    classify_log_surface,
    setup_file_logging,
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


@pytest.mark.parametrize(
    ("logger_name", "expected"),
    [
        ("gobby", "daemon"),
        ("gobby.runner", "daemon"),
        ("gobby.rule_allow_audit", "allow_audit"),
        ("gobby.hooks", "hooks"),
        ("gobby.hooks.events", "hooks"),
        ("gobby.hooks_extra", "daemon"),
        ("gobby.mcp", "mcp"),
        ("gobby.mcp.server", "mcp"),
        ("gobby.mcp_proxy", "mcp"),
        ("gobby.mcp_proxy.manager", "mcp"),
        ("gobby.servers.routes.mcp", "mcp"),
        ("gobby.servers.routes.mcp.tools", "mcp"),
        ("gobby.scheduler.executor", "automation"),
        ("gobby.dispatch.worker", "automation"),
        ("gobby.build.runner", "automation"),
        ("gobby.system_automation", "automation"),
        ("gobby.workflows.pipeline_heartbeat", "automation"),
        ("gobby.workflows.pipeline_executor", "automation"),
        ("gobby.workflows.pipeline.handlers", "automation"),
    ],
)
def test_classify_log_surface(logger_name: str, expected: str) -> None:
    assert classify_log_surface(logger_name) == expected


def test_setup_file_logging_routes_each_record_to_one_primary_surface(
    logging_config: LoggingSettings,
) -> None:
    setup_file_logging(logging_config)

    messages = {
        "gobby.runner": "daemon-record",
        "gobby.rule_allow_audit": '{"result":"allow"}',
        "gobby.hooks.events": "hook-record",
        "gobby.mcp_proxy.manager": "mcp-proxy-record",
        "gobby.servers.routes.mcp.tools": "mcp-route-record",
        "gobby.scheduler.executor": "scheduler-record",
        "gobby.dispatch.worker": "dispatch-record",
        "gobby.build.runner": "build-record",
        "gobby.system_automation": "system-automation-record",
        "gobby.workflows.pipeline_heartbeat": "pipeline-heartbeat-record",
        "gobby.workflows.pipeline_executor": "pipeline-executor-record",
        "gobby.workflows.pipeline.handlers": "pipeline-handler-record",
    }
    for logger_name, message in messages.items():
        logging.getLogger(logger_name).info(message)
    logging.getLogger("gobby.hooks.events").warning("hook-warning")
    logging.getLogger("gobby.scheduler.scheduler").warning("automation-warning")
    logging.getLogger("gobby.runner").info("daemon-info")

    paths = {
        "automation": resolved_log_path(logging_config, AUTOMATION_LOG_FILENAME),
        "daemon": resolved_log_path(logging_config, DAEMON_LOG_FILENAME),
        "errors": resolved_log_path(logging_config, ERRORS_LOG_FILENAME),
        "hooks": resolved_log_path(logging_config, HOOKS_LOG_FILENAME),
        "mcp": resolved_log_path(logging_config, MCP_LOG_FILENAME),
        "allow_audit": resolved_log_path(logging_config, RULE_ALLOW_AUDIT_LOG_FILENAME),
    }
    contents = {surface: path.read_text() for surface, path in paths.items()}

    assert "daemon-record" in contents["daemon"]
    assert "hook-record" in contents["hooks"]
    assert "mcp-proxy-record" in contents["mcp"]
    assert "mcp-route-record" in contents["mcp"]
    for message in (
        "scheduler-record",
        "dispatch-record",
        "build-record",
        "system-automation-record",
        "pipeline-heartbeat-record",
        "pipeline-executor-record",
        "pipeline-handler-record",
    ):
        assert message in contents["automation"]
    for message in messages.values():
        primary_writes = sum(
            message in contents[surface]
            for surface in ("allow_audit", "automation", "daemon", "hooks", "mcp")
        )
        assert primary_writes == 1

    assert contents["allow_audit"].strip() == '{"result":"allow"}'

    assert "hook-warning" in contents["hooks"]
    assert "hook-warning" in contents["errors"]
    assert "automation-warning" in contents["automation"]
    assert "automation-warning" in contents["errors"]
    assert "daemon-info" not in contents["errors"]
    assert not resolved_log_path(logging_config, RUNTIME_LOG_FILENAME).exists()
    for retired_name in (
        "gobby.log",
        "gobby-error.log",
        "hook-manager.log",
        "mcp-server.log",
        "mcp-client.log",
    ):
        assert not resolved_log_path(logging_config, retired_name).exists()


def test_setup_file_logging_isolates_early_failure_under_gobby_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    operator_home = tmp_path / "operator-home"
    isolated_home = tmp_path / "isolated-gobby"
    operator_logs = operator_home / ".gobby" / "logs"
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("GOBBY_HOME", str(isolated_home))
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    config = LoggingSettings()

    setup_file_logging(config, verbose=True)
    logging.getLogger("gobby.runner_lifecycle").error(
        "Fatal error: Qdrant configuration is missing; run `gobby install`"
    )

    isolated_logs = isolated_home / "logs"
    assert resolved_log_path(config, RUNTIME_LOG_FILENAME).parent == isolated_logs
    assert "Fatal error" in (isolated_logs / DAEMON_LOG_FILENAME).read_text()
    assert "Fatal error" in (isolated_logs / ERRORS_LOG_FILENAME).read_text()
    assert not operator_logs.exists()


def test_setup_file_logging_uses_root_handlers_and_shared_formatter_family(
    logging_config: LoggingSettings,
) -> None:
    setup_file_logging(logging_config)

    root_logger = logging.getLogger("gobby")
    file_handlers = [
        handler
        for handler in root_logger.handlers
        if isinstance(handler, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 6
    audit_handlers = [
        handler
        for handler in file_handlers
        if Path(handler.baseFilename).name == RULE_ALLOW_AUDIT_LOG_FILENAME
    ]
    assert len(audit_handlers) == 1
    assert type(audit_handlers[0].formatter) is logging.Formatter
    assert {
        type(handler.formatter) for handler in file_handlers if handler not in audit_handlers
    } == {OTelTraceFormatter}
    for name in ("gobby.hooks", "gobby.mcp", "gobby.mcp_proxy", "gobby.servers.routes.mcp"):
        child = logging.getLogger(name)
        assert child.propagate
        assert child.handlers == []


def test_setup_file_logging_reconfiguration_closes_replaced_handlers(
    tmp_path: Path,
) -> None:
    first = LoggingSettings(dir=str(tmp_path / "first"), level="debug")
    second = LoggingSettings(dir=str(tmp_path / "second"), level="debug")
    setup_file_logging(first)
    old_handlers = list(logging.getLogger("gobby").handlers)

    setup_file_logging(second)

    assert all(getattr(handler, "stream", None) is None for handler in old_handlers)
    logging.getLogger("gobby.runner").info("second-phase")
    assert "second-phase" in resolved_log_path(second, DAEMON_LOG_FILENAME).read_text()
    assert "second-phase" not in resolved_log_path(first, DAEMON_LOG_FILENAME).read_text()


def test_setup_file_logging_rotation(logging_config: LoggingSettings) -> None:
    # Set small max_size_mb for testing rotation
    logging_config.max_size_mb = 1  # 1MB
    logging_config.backup_count = 2

    setup_file_logging(logging_config)

    logger = logging.getLogger("gobby")
    # Write a lot of data
    large_msg = "x" * 1024 * 100  # 100KB
    for _ in range(15):  # 1.5MB total
        logger.info(large_msg)

    # Check if rotated file exists
    assert Path(f"{resolved_log_path(logging_config, DAEMON_LOG_FILENAME)}.1").exists()


def test_setup_file_logging_verbose_sets_debug(logging_config: LoggingSettings) -> None:
    logging_config.level = "info"
    setup_file_logging(logging_config, verbose=True)

    root_logger = logging.getLogger("gobby")
    assert root_logger.level == logging.DEBUG


def test_setup_file_logging_json_format(logging_config: LoggingSettings) -> None:
    logging_config.format = "json"
    setup_file_logging(logging_config)

    root_logger = logging.getLogger("gobby")
    handler = [
        h
        for h in root_logger.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and Path(h.baseFilename).name != RULE_ALLOW_AUDIT_LOG_FILENAME
    ][0]
    assert isinstance(handler.formatter, JsonOTelFormatter)


def test_setup_file_logging_suppresses_websockets_info(logging_config: LoggingSettings) -> None:
    logging.getLogger("websockets").setLevel(logging.INFO)
    logging.getLogger("websockets.server").setLevel(logging.INFO)

    setup_file_logging(logging_config)

    assert logging.getLogger("websockets").level == logging.WARNING
    assert logging.getLogger("websockets.server").level == logging.WARNING


def test_setup_file_logging_has_no_otel_log_handler(logging_config: LoggingSettings) -> None:
    from opentelemetry.sdk._logs import LoggingHandler

    setup_file_logging(logging_config)

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
    telemetry_config.llm_tracing.providers = ["openai"]

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="'asyncio.iscoroutinefunction' is deprecated.*",
            category=DeprecationWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message="'return' in a 'finally' block",
            category=SyntaxWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message="Support for class-based `config` is deprecated.*",
            category=DeprecationWarning,
        )
        from opentelemetry.instrumentation.openai import OpenAIInstrumentor

        instrumentor = OpenAIInstrumentor()
        try:
            daemon_init_telemetry(telemetry_config, logging_config)

            assert instrumentor.is_instrumented_by_opentelemetry
        finally:
            instrumentor.uninstrument()
            _instrumented.discard("openai")


def test_shutdown_telemetry_has_no_logging_bridge_and_shuts_down_providers() -> None:
    with patch("gobby.telemetry.shutdown_providers") as mock_shutdown_providers:
        shutdown_telemetry()

    assert not hasattr(telemetry, "LoggingInstrumentor")
    mock_shutdown_providers.assert_called_once()


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
