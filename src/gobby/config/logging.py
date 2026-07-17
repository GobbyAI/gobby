"""Dedicated logging settings and path resolution."""

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAIN_LOG_FILENAME = "gobby.log"
ERROR_LOG_FILENAME = "gobby-error.log"
STDERR_LOG_FILENAME = "gobby-stderr.log"
HOOK_MANAGER_LOG_FILENAME = "hook-manager.log"
MCP_SERVER_LOG_FILENAME = "mcp-server.log"
MCP_CLIENT_LOG_FILENAME = "mcp-client.log"
UI_LOG_FILENAME = "ui.log"


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
        default="~/.gobby/logs",
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

    @field_validator(
        "max_size_mb",
        "backup_count",
        "runtime_max_size_mb",
        "growth_warn_mb_per_interval",
    )
    @classmethod
    def validate_positive(cls, value: int) -> int:
        """Require positive sizes and retention counts."""
        if value <= 0:
            raise ValueError("Value must be positive")
        return value


def resolved_logs_dir(config: LoggingSettings) -> Path:
    """Return the expanded logs directory."""
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
