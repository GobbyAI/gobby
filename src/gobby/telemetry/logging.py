"""
Unified logging configuration using OpenTelemetry logging bridge.

Sets up standard file logging with rotation and bridges to OpenTelemetry
for unified tracing and structured logging.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggingHandler
from opentelemetry.trace import format_trace_id

from gobby.telemetry.providers import get_logger_provider

if TYPE_CHECKING:
    from gobby.telemetry.config import TelemetrySettings


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

        return json.dumps(log_data)


def setup_otel_logging(config: TelemetrySettings, verbose: bool = False) -> None:
    """
    Configure rotating file logging with OpenTelemetry bridge.

    Replaces legacy file logging and setup_mcp_logging.

    Args:
        config: TelemetrySettings instance.
        verbose: If True, set level to DEBUG regardless of config.
    """
    # 1. Determine log level
    if verbose:
        level = logging.DEBUG
    else:
        level = getattr(logging, config.log_level.upper(), logging.INFO)

    # 2. Configure formatters
    formatter: logging.Formatter
    if config.log_format == "json":
        formatter = JsonOTelFormatter(datefmt="%Y-%m-%dT%H:%M:%S")
    else:
        log_format = "%(asctime)s - %(levelname)-8s - %(short_name)s.%(funcName)s - %(message)s"
        formatter = OTelTraceFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    # 3. Create file handlers for all 6 paths
    max_bytes = config.max_size_mb * 1024 * 1024
    backup_count = config.backup_count

    # Mapping of log names to config paths
    log_paths = {
        "gobby": config.log_file,
        "gobby-error": config.log_file_error,
        "hook-manager": config.log_file_hook_manager,
        "mcp-server": config.log_file_mcp_server,
        "mcp-client": config.log_file_mcp_client,
    }

    # Map logger names to their corresponding files
    logger_mapping = {
        "gobby": ["gobby", "gobby-error"],
        "gobby.hooks": ["hook-manager"],
        "gobby.mcp.server": ["mcp-server"],
        "gobby.mcp.client": ["mcp-client"],
    }

    # Helper to create handler
    def create_handler(path_str: str, log_level: int) -> RotatingFileHandler:
        p = Path(path_str).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(
            str(p),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        h.setLevel(log_level)
        h.setFormatter(formatter)
        return h

    # 4. Configure loggers
    root_logger = logging.getLogger("gobby")
    root_logger.setLevel(level)
    root_logger.propagate = False

    # Remove old handlers
    for h in root_logger.handlers[:]:
        h.close()
        root_logger.removeHandler(h)

    # Main and Error logs on root gobby logger
    root_logger.addHandler(create_handler(config.log_file, level))
    root_logger.addHandler(create_handler(config.log_file_error, logging.ERROR))

    # Other loggers
    for logger_name, log_keys in logger_mapping.items():
        if logger_name == "gobby":
            continue

        logger = logging.getLogger(logger_name)
        logger.setLevel(level)
        logger.propagate = False
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)

        for key in log_keys:
            logger.addHandler(create_handler(log_paths[key], level))

    # 5. Bridge Python logging to OpenTelemetry
    logger_provider = get_logger_provider(config)
    otel_handler = LoggingHandler(level=level, logger_provider=logger_provider)
    root_logger.addHandler(otel_handler)


def init_telemetry(config: TelemetrySettings, verbose: bool = False) -> None:
    """
    Initialize all telemetry providers and logging.

    Args:
        config: TelemetrySettings instance.
        verbose: Verbose logging flag.
    """
    from opentelemetry import metrics, trace

    from gobby.telemetry.providers import (
        get_meter_provider,
        get_tracer_provider,
    )

    # Init and set global providers
    tracer_provider = get_tracer_provider(config)
    trace.set_tracer_provider(tracer_provider)

    meter_provider = get_meter_provider(config)
    metrics.set_meter_provider(meter_provider)

    # Setup logging bridge
    setup_otel_logging(config, verbose=verbose)
