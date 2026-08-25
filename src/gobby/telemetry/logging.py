"""Formatted application logging, routing, and parser diagnostics."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, ClassVar, Literal

from opentelemetry import trace
from opentelemetry.trace import format_trace_id

from gobby.config.logging import (
    AUTOMATION_LOG_FILENAME,
    DAEMON_LOG_FILENAME,
    ERRORS_LOG_FILENAME,
    HOOKS_LOG_FILENAME,
    MCP_LOG_FILENAME,
    RULE_ALLOW_AUDIT_LOG_FILENAME,
    LoggingSettings,
    allow_audit_backup_count,
    resolved_logs_dir,
)

LogSurface = Literal["daemon", "hooks", "mcp", "automation", "allow_audit"]

_PARSER_ERROR_NAMESPACE = "gobby.parser_error"
_PRIMARY_LOG_FILENAMES: dict[LogSurface, str] = {
    "allow_audit": RULE_ALLOW_AUDIT_LOG_FILENAME,
    "automation": AUTOMATION_LOG_FILENAME,
    "daemon": DAEMON_LOG_FILENAME,
    "hooks": HOOKS_LOG_FILENAME,
    "mcp": MCP_LOG_FILENAME,
}
_MANAGED_CHILD_LOGGERS = (
    "gobby.rule_allow_audit",
    "gobby.hooks",
    "gobby.mcp",
    "gobby.mcp.server",
    "gobby.mcp.client",
    "gobby.mcp_proxy",
    "gobby.servers.routes.mcp",
)
_MCP_NAMESPACES = ("gobby.mcp", "gobby.mcp_proxy", "gobby.servers.routes.mcp")
_AUTOMATION_NAMESPACES = (
    "gobby.scheduler",
    "gobby.dispatch",
    "gobby.build",
    "gobby.system_automation",
    "gobby.workflows.pipeline",
    "gobby.workflows.pipeline_executor",
    "gobby.workflows.pipeline_heartbeat",
)


@dataclass(frozen=True)
class _HandlerConfig:
    logs_dir: Path
    max_bytes: int
    backup_count: int
    allow_audit_max_bytes: int
    allow_audit_backup_count: int


_default_logging_settings = LoggingSettings()
_active_handler_config = _HandlerConfig(
    logs_dir=resolved_logs_dir(_default_logging_settings),
    max_bytes=_default_logging_settings.max_size_mb * 1024 * 1024,
    backup_count=_default_logging_settings.backup_count,
    allow_audit_max_bytes=_default_logging_settings.allow_audit_max_size_mb * 1024 * 1024,
    allow_audit_backup_count=allow_audit_backup_count(_default_logging_settings),
)
_registered_parser_loggers: set[str] = set()
_logging_metric_handler: _LoggingMetricHandler | None = None


def _in_namespace(logger_name: str, namespace: str) -> bool:
    return logger_name == namespace or logger_name.startswith(f"{namespace}.")


def classify_log_surface(logger_name: str) -> LogSurface:
    """Return the semantic primary surface for a Gobby logger name."""
    if _in_namespace(logger_name, "gobby.rule_allow_audit"):
        return "allow_audit"
    if _in_namespace(logger_name, "gobby.hooks"):
        return "hooks"
    if any(_in_namespace(logger_name, namespace) for namespace in _MCP_NAMESPACES):
        return "mcp"
    if any(_in_namespace(logger_name, namespace) for namespace in _AUTOMATION_NAMESPACES):
        return "automation"
    return "daemon"


class _LoggingMetricHandler(logging.Handler):
    """Count bounded WARNING+ source records without emitting diagnostics."""

    _severity_by_level: ClassVar[dict[int, str]] = {
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self._emitting = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        severity = self._severity_by_level.get(record.levelno)
        if severity is None or getattr(self._emitting, "active", False):
            return

        surface = (
            "parser"
            if _in_namespace(record.name, _PARSER_ERROR_NAMESPACE)
            else classify_log_surface(record.name)
        )
        self._emitting.active = True
        try:
            from gobby.telemetry.health_metrics import record_logging_record

            record_logging_record(surface, severity)
        except Exception:
            pass
        finally:
            self._emitting.active = False


def configure_logging_metrics(*, enabled: bool) -> None:
    """Attach one health metric handler to the Gobby logging root."""
    global _logging_metric_handler

    root_logger = logging.getLogger("gobby")
    existing = [
        existing_handler
        for existing_handler in root_logger.handlers
        if isinstance(existing_handler, _LoggingMetricHandler)
    ]
    if not enabled:
        for existing_handler in existing:
            root_logger.removeHandler(existing_handler)
            existing_handler.close()
        if _logging_metric_handler is not None and _logging_metric_handler not in existing:
            _logging_metric_handler.close()
        _logging_metric_handler = None
        return

    metric_handler = existing[0] if existing else _LoggingMetricHandler()
    if _logging_metric_handler is not None and _logging_metric_handler is not metric_handler:
        _logging_metric_handler.close()
    for duplicate in existing[1:]:
        root_logger.removeHandler(duplicate)
        duplicate.close()
    if metric_handler not in root_logger.handlers:
        root_logger.addHandler(metric_handler)
    _logging_metric_handler = metric_handler


def _routed_primary_surface(logger_name: str) -> LogSurface:
    surface = classify_log_surface(logger_name)
    if surface in _PRIMARY_LOG_FILENAMES:
        return surface
    return "daemon"


class _PrimarySurfaceFilter(logging.Filter):
    def __init__(self, surface: LogSurface) -> None:
        super().__init__()
        self.surface = surface

    def filter(self, record: logging.LogRecord) -> bool:
        if _in_namespace(record.name, _PARSER_ERROR_NAMESPACE):
            return False
        return _routed_primary_surface(record.name) == self.surface


class OTelTraceFormatter(logging.Formatter):
    """
    Formatter that injects OpenTelemetry trace ID into log records.

    Replaces the legacy formatter with OTel support.
    """

    STANDARD_ATTRS: ClassVar[set[str]] = {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "thread",
        "threadName",
        "exc_info",
        "exc_text",
        "stack_info",
        "asctime",
        "trace_id",
        "span_id",
        "short_name",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        """Format log record including trace_id and extra fields."""
        # Inject OTel trace ID if active
        span = trace.get_current_span()
        if span and span.get_span_context().is_valid:
            trace_id = format_trace_id(span.get_span_context().trace_id)
        else:
            trace_id = "-"

        # Short name for gobby loggers
        if record.name.startswith("gobby."):
            short_name = record.name[6:]
        else:
            short_name = record.name

        # Store on record for format string interpolation
        record.__dict__["trace_id"] = trace_id
        record.__dict__["short_name"] = short_name

        # Standard formatting
        base_msg = super().format(record)

        # Append trace ID only when a real span is active
        if trace_id != "-":
            base_msg = f"{base_msg} [{trace_id}]"

        # Append extra fields (from record.__dict__ that are not standard)
        extra_fields = {}
        for key, value in record.__dict__.items():
            if key not in self.STANDARD_ATTRS and not key.startswith("_"):
                extra_fields[key] = value

        if extra_fields:
            extra_str = " | ".join(f"{k}={v}" for k, v in extra_fields.items())
            return f"{base_msg} | {extra_str}"

        return base_msg


class JsonOTelFormatter(logging.Formatter):
    """
    JSON formatter with OpenTelemetry trace and span ID support.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        span = trace.get_current_span()
        trace_id = None
        span_id = None
        if span and span.get_span_context().is_valid:
            trace_id = format_trace_id(span.get_span_context().trace_id)
            span_id = format(span.get_span_context().span_id, "016x")

        log_data: dict[str, Any] = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "name": record.name,
            "module": record.module,
            "func": record.funcName,
            "message": record.getMessage(),
        }

        if trace_id:
            log_data["trace_id"] = trace_id
        if span_id:
            log_data["span_id"] = span_id

        # Add extra fields
        for key, value in record.__dict__.items():
            if key not in OTelTraceFormatter.STANDARD_ATTRS and not key.startswith("_"):
                log_data[key] = value

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, default=str)


def _handler_config(settings: LoggingSettings) -> _HandlerConfig:
    return _HandlerConfig(
        logs_dir=resolved_logs_dir(settings),
        max_bytes=settings.max_size_mb * 1024 * 1024,
        backup_count=settings.backup_count,
        allow_audit_max_bytes=settings.allow_audit_max_size_mb * 1024 * 1024,
        allow_audit_backup_count=allow_audit_backup_count(settings),
    )


def _create_rotating_handler(
    path: Path,
    config: _HandlerConfig,
    *,
    level: int,
    formatter: logging.Formatter,
) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def _replace_handlers(target: logging.Logger, handlers: list[logging.Handler]) -> None:
    old_handlers = list(target.handlers)
    for handler in old_handlers:
        target.removeHandler(handler)
    for handler in old_handlers:
        if handler not in handlers:
            handler.close()
    for handler in handlers:
        target.addHandler(handler)


def _formatted_log_formatter(settings: LoggingSettings) -> logging.Formatter:
    if settings.format == "json":
        return JsonOTelFormatter(datefmt="%Y-%m-%dT%H:%M:%S")
    log_format = "%(asctime)s - %(levelname)-8s - %(short_name)s.%(funcName)s - %(message)s"
    return OTelTraceFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")


def parser_error_log_path(cli_name: str) -> Path:
    """Return the active parser diagnostic path for a CLI."""
    return _effective_handler_config().logs_dir / f"{cli_name}-parser-error.log"


def _effective_handler_config() -> _HandlerConfig:
    configured_dir = os.environ.get("GOBBY_LOGGING_DIR")
    logs_dir = Path(configured_dir) if configured_dir else _active_handler_config.logs_dir
    return _HandlerConfig(
        logs_dir=logs_dir.expanduser(),
        max_bytes=_active_handler_config.max_bytes,
        backup_count=_active_handler_config.backup_count,
        allow_audit_max_bytes=_active_handler_config.allow_audit_max_bytes,
        allow_audit_backup_count=_active_handler_config.allow_audit_backup_count,
    )


def _rotating_handler_matches(
    handler: logging.Handler,
    path: Path,
    config: _HandlerConfig,
) -> bool:
    return (
        isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename).resolve() == path.resolve()
        and handler.maxBytes == config.max_bytes
        and handler.backupCount == config.backup_count
    )


def _configure_parser_error_logger(logger_name: str) -> logging.Logger:
    cli_name = logger_name.removeprefix(f"{_PARSER_ERROR_NAMESPACE}.")
    config = _effective_handler_config()
    path = parser_error_log_path(cli_name)
    parser_logger = logging.getLogger(logger_name)
    parser_logger.setLevel(logging.INFO)
    parser_logger.propagate = True

    if len(parser_logger.handlers) == 1 and _rotating_handler_matches(
        parser_logger.handlers[0], path, config
    ):
        return parser_logger

    try:
        handler: logging.Handler = _create_rotating_handler(
            path,
            config,
            level=logging.INFO,
            formatter=logging.Formatter("%(message)s"),
        )
    except OSError:
        logging.getLogger(__name__).debug(
            "Failed to configure transcript parser error log",
            extra={"cli": cli_name, "path": str(path)},
            exc_info=True,
        )
        handler = logging.NullHandler()
    _replace_handlers(parser_logger, [handler])
    return parser_logger


def get_parser_error_logger(cli_name: str) -> logging.Logger:
    """Return a dedicated parser logger configured for the active logging directory."""
    logger_name = f"{_PARSER_ERROR_NAMESPACE}.{cli_name}"
    _registered_parser_loggers.add(logger_name)
    return _configure_parser_error_logger(logger_name)


def _create_formatted_handlers(
    config: _HandlerConfig,
    level: int,
    formatter: logging.Formatter,
) -> list[logging.Handler]:
    handlers: list[logging.Handler] = []
    try:
        for surface, filename in _PRIMARY_LOG_FILENAMES.items():
            surface_config = config
            surface_formatter = formatter
            if surface == "allow_audit":
                surface_config = _HandlerConfig(
                    logs_dir=config.logs_dir,
                    max_bytes=config.allow_audit_max_bytes,
                    backup_count=config.allow_audit_backup_count,
                    allow_audit_max_bytes=config.allow_audit_max_bytes,
                    allow_audit_backup_count=config.allow_audit_backup_count,
                )
                surface_formatter = logging.Formatter("%(message)s")
            handler = _create_rotating_handler(
                config.logs_dir / filename,
                surface_config,
                level=level,
                formatter=surface_formatter,
            )
            handler.addFilter(_PrimarySurfaceFilter(surface))
            handlers.append(handler)
        handlers.append(
            _create_rotating_handler(
                config.logs_dir / ERRORS_LOG_FILENAME,
                config,
                level=logging.WARNING,
                formatter=formatter,
            )
        )
    except Exception:
        for created_handler in handlers:
            created_handler.close()
        raise
    return handlers


def setup_file_logging(config: LoggingSettings, verbose: bool = False) -> None:
    """Configure exclusive primary routing and WARNING+ aggregation for Gobby records."""
    global _active_handler_config

    level = logging.DEBUG if verbose else getattr(logging, config.level.upper(), logging.INFO)
    handler_config = _handler_config(config)
    handlers = _create_formatted_handlers(
        handler_config,
        level,
        _formatted_log_formatter(config),
    )
    if _logging_metric_handler is not None:
        handlers.append(_logging_metric_handler)

    root_logger = logging.getLogger("gobby")
    root_logger.setLevel(level)
    root_logger.propagate = False
    _replace_handlers(root_logger, handlers)

    for logger_name in _MANAGED_CHILD_LOGGERS:
        child_logger = logging.getLogger(logger_name)
        child_logger.setLevel(logging.NOTSET)
        child_logger.propagate = True
        _replace_handlers(child_logger, [])

    _active_handler_config = handler_config
    for logger_name in _registered_parser_loggers:
        _configure_parser_error_logger(logger_name)

    for logger_name in ("websockets", "websockets.server"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    from gobby.telemetry.rule_allow_audit import configure_rule_allow_audit

    configure_rule_allow_audit(config)
