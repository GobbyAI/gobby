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

    @field_validator("debounce_interval", "poll_interval")
    @classmethod
    def validate_positive_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("interval must be greater than zero")
        return value
