"""
Logging utilities for request tracking and structured logging.

Provides request ID tracking, context propagation, custom log adapters,
and file-based logging configuration.
"""

import contextvars
import logging
import logging.handlers
import uuid
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, ClassVar

# Context variable for tracking request IDs across async operations
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


class RequestIDFilter(logging.Filter):
    """Add request ID to log records if available."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add request_id attribute to log record."""
        request_id = request_id_var.get()
        record.request_id = request_id if request_id else "-"
        return True


class ContextLogger(logging.LoggerAdapter):
    """
    Logger adapter that adds contextual information to log records.

    Supports adding request_id, operation, duration_ms, and other metadata.
    """

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        """Add extra context to log record."""
        extra = kwargs.get("extra", {})

        # Add request ID if available
        request_id = request_id_var.get()
        if request_id:
            extra["request_id"] = request_id

        # Merge with any existing extra data
        if self.extra:
            extra.update(self.extra)

        kwargs["extra"] = extra
        return msg, kwargs


def generate_request_id() -> str:
    """Generate a unique request ID."""
    return str(uuid.uuid4())


def set_request_id(request_id: str | None = None) -> str:
    """
    Set the request ID for the current context.

    Args:
        request_id: Request ID to set. If None, generates a new one.

    Returns:
        The request ID that was set.
    """
    if request_id is None:
        request_id = generate_request_id()
    request_id_var.set(request_id)
    return request_id


def get_request_id() -> str | None:
    """Get the current request ID from context."""
    return request_id_var.get()


def clear_request_id() -> None:
    """Clear the request ID from context."""
    request_id_var.set(None)


def get_context_logger(name: str, extra: dict[str, Any] | None = None) -> ContextLogger:
    """
    Get a logger with context support.

    Args:
        name: Logger name (usually __name__)
        extra: Additional context to include in all log messages

    Returns:
        ContextLogger instance
    """
    logger = logging.getLogger(name)
    return ContextLogger(logger, extra or {})


class ExtraFieldsFormatter(logging.Formatter):
    """
    Custom formatter that includes extra fields in log output.

    Formats log records to include any extra fields passed via the 'extra'
    parameter, making debugging easier by showing all context.
    """

    # Standard logging record attributes to exclude from extra fields
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
        "request_id",
        "short_name",
    }

    def format(self, record: logging.LogRecord) -> str:
        """Format log record including extra fields."""
        # Strip gobby. prefix from logger name for cleaner output
        # e.g., "gobby.http_server" -> "http_server"
        # Use short_name attribute to avoid mutating record.name (which leaks to other handlers)
        if record.name.startswith("gobby."):
            record.short_name = record.name[13:]  # len("gobby.") = 13
        else:
            record.short_name = record.name

        # Format the base message
        base_msg = super().format(record)

        # Collect extra fields
        extra_fields = {}
        for key, value in record.__dict__.items():
            if key not in self.STANDARD_ATTRS and not key.startswith("_"):
                extra_fields[key] = value

        # Append extra fields if any exist
        if extra_fields:
            extra_str = " | ".join(f"{k}={v}" for k, v in extra_fields.items())
            return f"{base_msg} | {extra_str}"

        return base_msg


def setup_file_logging(verbose: bool = False) -> None:
    """
    Configure rotating file handlers for logging.

    Sets up two log files:
    1. Main log: All messages (level from config, or DEBUG if verbose flag set)
    2. Error log: Only ERROR and CRITICAL messages

    Args:
        verbose: If True, override config level to DEBUG

    Log file paths and rotation settings are loaded from ~/.gobby/config.yaml:
        - logging.level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        - logging.format: Log format (text or json)
        - logging.client: Main log file path
        - logging.client_error: Error log file path
        - logging.max_size_mb: Max file size before rotation
        - logging.backup_count: Number of backup files to keep
    """
    # Load config to get log file paths and rotation settings
    from gobby.config.app import load_config

    config = load_config()

    # Expand paths and ensure log directory exists
    log_file_path = Path(config.logging.client).expanduser()
    error_log_file_path = Path(config.logging.client_error).expanduser()

    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    error_log_file_path.parent.mkdir(parents=True, exist_ok=True)

    # Get rotation settings from config
    max_bytes = config.logging.max_size_mb * 1024 * 1024
    backup_count = config.logging.backup_count

    # Get the gobby logger (package-level)
    pkg_logger = logging.getLogger("gobby")

    # Get logging level from config (verbose flag overrides to DEBUG)
    if verbose:
        log_level = logging.DEBUG
    else:
        config_level = getattr(config.logging, "level", "INFO").upper()
        log_level = getattr(logging, config_level, logging.INFO)
    pkg_logger.setLevel(log_level)

    # Remove any existing handlers to avoid duplicates
    pkg_logger.handlers.clear()

    # Get log format from config (text or json)
    log_format_type = getattr(config.logging, "format", "text").lower()

    # Create formatter based on config format
    if log_format_type == "json":
        # JSON format for structured logging
        log_format = '{"time": "%(asctime)s", "level": "%(levelname)s", "module": "%(short_name)s", "func": "%(funcName)s", "message": "%(message)s"}'
        formatter = ExtraFieldsFormatter(log_format, datefmt="%Y-%m-%dT%H:%M:%S")
    else:
        # Text format (default) - human readable
        log_format = "%(asctime)s - %(levelname)-8s - %(short_name)s.%(funcName)s - %(message)s"
        formatter = ExtraFieldsFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    # Create request ID filter
    request_id_filter = RequestIDFilter()

    # Create main log handler with rotation
    main_handler = logging.handlers.RotatingFileHandler(
        filename=str(log_file_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    main_handler.setLevel(log_level)
    main_handler.setFormatter(formatter)
    main_handler.addFilter(request_id_filter)
    pkg_logger.addHandler(main_handler)

    # Create error log handler (ERROR and above only)
    error_handler = logging.handlers.RotatingFileHandler(
        filename=str(error_log_file_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    error_handler.addFilter(request_id_filter)
    pkg_logger.addHandler(error_handler)

    # Prevent propagation to root logger to avoid duplicate logs
    pkg_logger.propagate = False

    # Log setup confirmation
    logger = logging.getLogger(__name__)
    logger.debug(
        f"File logging configured (level={logging.getLevelName(log_level)}, "
        f"main_log={log_file_path}, error_log={error_log_file_path})"
    )


def setup_mcp_logging(verbose: bool = False) -> tuple[logging.Logger, logging.Logger]:
    """
    Configure separate loggers for MCP server and client operations.

    Sets up dedicated log files for:
    1. MCP Server: Logs for starting/stopping the MCP server, tool registration
    2. MCP Client: Logs for connecting to downstream servers, proxy operations

    Args:
        verbose: If True, override config level to DEBUG

    Returns:
        Tuple of (mcp_server_logger, mcp_client_logger)

    Log file paths are loaded from ~/.gobby/config.yaml:
        - logging.mcp_server: MCP server log file path
        - logging.mcp_client: MCP client log file path
    """
    from gobby.config.app import load_config

    config = load_config()

    # Get log file paths from config
    mcp_server_log_path = Path(config.logging.mcp_server).expanduser()
    mcp_client_log_path = Path(config.logging.mcp_client).expanduser()

    # Ensure directories exist
    mcp_server_log_path.parent.mkdir(parents=True, exist_ok=True)
    mcp_client_log_path.parent.mkdir(parents=True, exist_ok=True)

    # Get rotation settings from config
    max_bytes = config.logging.max_size_mb * 1024 * 1024
    backup_count = config.logging.backup_count

    # Get logging level from config (verbose flag overrides to DEBUG)
    if verbose:
        log_level = logging.DEBUG
    else:
        config_level = getattr(config.logging, "level", "INFO").upper()
        log_level = getattr(logging, config_level, logging.INFO)

    # Get log format from config
    log_format_type = getattr(config.logging, "format", "text").lower()

    if log_format_type == "json":
        log_format = '{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}'
        date_format = "%Y-%m-%dT%H:%M:%S"
    else:
        log_format = "%(asctime)s - %(levelname)-8s - %(name)s - %(message)s"
        date_format = "%Y-%m-%d %H:%M:%S"

    formatter = logging.Formatter(log_format, datefmt=date_format)

    # Setup MCP Server logger
    mcp_server_logger = logging.getLogger("gobby.mcp.server")
    mcp_server_logger.setLevel(log_level)

    # Clear existing handlers to avoid duplicates
    mcp_server_logger.handlers.clear()

    mcp_server_handler = logging.handlers.RotatingFileHandler(
        filename=str(mcp_server_log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    mcp_server_handler.setLevel(log_level)
    mcp_server_handler.setFormatter(formatter)
    mcp_server_logger.addHandler(mcp_server_handler)
    mcp_server_logger.propagate = False  # Don't propagate to avoid duplicate logs

    # Setup MCP Client logger
    mcp_client_logger = logging.getLogger("gobby.mcp.client")
    mcp_client_logger.setLevel(log_level)

    # Clear existing handlers to avoid duplicates
    mcp_client_logger.handlers.clear()

    mcp_client_handler = logging.handlers.RotatingFileHandler(
        filename=str(mcp_client_log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    mcp_client_handler.setLevel(log_level)
    mcp_client_handler.setFormatter(formatter)
    mcp_client_logger.addHandler(mcp_client_handler)
    mcp_client_logger.propagate = False  # Don't propagate to avoid duplicate logs

    # Log setup confirmation
    mcp_server_logger.debug(f"MCP server logging configured (path={mcp_server_log_path})")
    mcp_client_logger.debug(f"MCP client logging configured (path={mcp_client_log_path})")

    return mcp_server_logger, mcp_client_logger


def get_mcp_server_logger() -> logging.Logger:
    """Get the MCP server logger (creates if not configured)."""
    return logging.getLogger("gobby.mcp.server")


def get_mcp_client_logger() -> logging.Logger:
    """Get the MCP client logger (creates if not configured)."""
    return logging.getLogger("gobby.mcp.client")
