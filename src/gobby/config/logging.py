"""Dedicated logging settings and path resolution."""

from collections.abc import Mapping
from math import ceil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from gobby.paths import get_gobby_home

AUTOMATION_LOG_FILENAME = "automation.log"
DAEMON_LOG_FILENAME = "daemon.log"
ERRORS_LOG_FILENAME = "errors.log"
RUNTIME_LOG_FILENAME = "runtime.log"
HOOKS_LOG_FILENAME = "hooks.log"
LOOP_LAG_LOG_FILENAME = "loop_lag.jsonl"
MCP_LOG_FILENAME = "mcp.log"
RULE_ALLOW_AUDIT_LOG_FILENAME = "rule-allow-audit.jsonl"
UI_LOG_FILENAME = "ui.log"

_DEFAULT_LOG_DIR = "~/.gobby/logs"

ALLOW_AUDIT_EVENTS_PER_DAY = 368_000
ALLOW_AUDIT_SIZING_BYTES_PER_LINE = 512


class LoggingSettings(BaseModel):
    """Application and runtime logging configuration."""

    level: Literal["debug", "info", "warning", "error"] = Field(
        default="info",
        description="Log level",
    )
    format: Literal["text", "json"] = Field(
        default="text",
        description="Log format",
    )
    dir: str = Field(
        default=_DEFAULT_LOG_DIR,
        description="Directory containing Gobby log files",
    )
    max_size_mb: int = Field(
        default=10,
        description="Maximum rotating log file size in MB",
    )
    backup_count: int = Field(
        default=5,
        description="Number of rotated log files to keep",
    )
    runtime_max_size_mb: int = Field(
        default=50,
        description="Maximum runtime output file size in MB",
    )
    growth_warn_mb_per_interval: int = Field(
        default=100,
        description="Logs directory growth threshold per resource-monitor interval",
    )
    allow_audit_retention_days: int = Field(
        default=14,
        ge=14,
        description="Target retention in days for durable rule allow audit lines",
    )
    allow_audit_max_size_mb: int = Field(
        default=256,
        description="Maximum size of each rule allow audit rotation",
    )
    allow_audit_queue_capacity: int = Field(
        default=8192,
        description="Maximum queued rule allow audit lines; overflow drops newest",
    )
    allow_audit_shutdown_timeout_seconds: float = Field(
        default=2.0,
        description="Hard deadline for draining queued rule allow audit lines",
    )

    @field_validator(
        "max_size_mb",
        "backup_count",
        "runtime_max_size_mb",
        "growth_warn_mb_per_interval",
        "allow_audit_max_size_mb",
        "allow_audit_queue_capacity",
    )
    @classmethod
    def validate_positive(cls, value: int) -> int:
        """Require positive sizes and retention counts."""
        if value <= 0:
            raise ValueError("Value must be positive")
        return value

    @field_validator("allow_audit_shutdown_timeout_seconds")
    @classmethod
    def validate_positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("Value must be positive")
        return value


def allow_audit_backup_count(config: LoggingSettings) -> int:
    """Size rotations for the configured day target.

    The planning sample measured 368,000 allow lines/day. At a conservative
    512 bytes/line, 14 days need 2,637,824,000 bytes. Ten 256 MiB files cover
    2,684,354,560 bytes, so the default keeps nine backups plus the active file.
    """
    required_bytes = (
        config.allow_audit_retention_days
        * ALLOW_AUDIT_EVENTS_PER_DAY
        * ALLOW_AUDIT_SIZING_BYTES_PER_LINE
    )
    max_bytes = config.allow_audit_max_size_mb * 1024 * 1024
    return max(1, ceil(required_bytes / max_bytes) - 1)


def resolved_logs_dir(config: LoggingSettings) -> Path:
    """Return the expanded logs directory."""
    if config.dir == _DEFAULT_LOG_DIR:
        return get_gobby_home() / "logs"
    return Path(config.dir).expanduser()


def resolved_log_path(config: LoggingSettings, filename: str) -> Path:
    """Return a fixed log filename under the configured logs directory."""
    return resolved_logs_dir(config) / filename


def common_log_parent(paths: Mapping[str, str]) -> Path:
    """Resolve one parent directory from named legacy log paths."""
    resolved = {name: Path(value).expanduser().parent for name, value in paths.items()}
    parents = set(resolved.values())
    if len(parents) > 1:
        details = ", ".join(f"{name}={paths[name]!r}" for name in paths)
        raise ValueError(f"Conflicting log directories: {details}")
    if not resolved:
        raise ValueError("At least one log path is required")
    return next(iter(resolved.values()))
