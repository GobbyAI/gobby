from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class WikiRootConfig(BaseModel):
    """One wiki root watched by the daemon."""

    scope: str = Field(description="Stable scope name for grouped watcher changes.")
    path: Path = Field(description="Wiki root path to watch.")

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        scope = value.strip()
        if not scope:
            raise ValueError("scope must not be empty")
        return scope

    @field_validator("path")
    @classmethod
    def expand_path(cls, value: Path) -> Path:
        return value.expanduser()


class WikiConfig(BaseModel):
    """Daemon wiki watcher configuration."""

    enabled: bool = Field(default=True, description="Enable daemon wiki file watching.")
    roots: list[WikiRootConfig] = Field(
        default_factory=list,
        description="Project and topic wiki roots to watch.",
    )
    debounce_interval: float = Field(
        default=0.5,
        description="Seconds to wait after a burst before handing changes to indexing.",
    )
    poll_interval: float = Field(
        default=0.25,
        description="Seconds between filesystem scans.",
    )
    ignore_globs: list[str] = Field(
        default_factory=lambda: ["outputs/**", "meta/health/**"],
        description="Root-relative file globs ignored by the watcher.",
    )
    codewiki_on_commit: bool = Field(
        default=False,
        description=(
            "Refresh generated codewiki docs after post-commit code indexing. Runtime "
            "config key wiki.codewiki_on_commit is canonical after startup."
        ),
    )
    codewiki_nightly_enabled: bool = Field(
        default=False,
        description="Refresh generated codewiki docs on a nightly daemon cron schedule.",
    )
    codewiki_nightly_schedule_cron: str = Field(
        default="0 3 * * *",
        description=(
            "Cron expression for nightly codewiki refresh, interpreted in the configured "
            "or host-local timezone. Execution timestamps are stored in UTC."
        ),
    )
    codewiki_nightly_timezone: str | None = Field(
        default=None,
        description=(
            "IANA timezone for nightly codewiki refresh scheduling. Unset uses the "
            "daemon host's local timezone when it can be resolved, otherwise UTC."
        ),
    )

    @field_validator("debounce_interval", "poll_interval")
    @classmethod
    def validate_positive_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("interval must be greater than zero")
        return value
