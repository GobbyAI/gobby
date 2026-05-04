"""Configuration for managed native binary freshness checks."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class BinFreshnessConfig(BaseModel):
    """Daemon background updater configuration for managed native binaries."""

    enabled: bool = Field(
        default=True,
        description="Enable GitHub-backed freshness checks for managed native binaries.",
    )
    initial_delay_seconds: float = Field(
        default=30.0,
        description="Delay before the first bin freshness check after daemon startup.",
    )
    interval_seconds: float = Field(
        default=3600.0,
        description="Base interval between bin freshness checks.",
    )
    jitter_seconds: float = Field(
        default=300.0,
        description="Maximum random jitter added to each interval.",
    )
    github_timeout_seconds: float = Field(
        default=30.0,
        description="Timeout for GitHub release metadata and artifact downloads.",
    )

    @field_validator("initial_delay_seconds", "jitter_seconds")
    @classmethod
    def validate_non_negative_seconds(cls, value: float) -> float:
        """Validate delay and jitter durations."""
        if value < 0:
            raise ValueError("duration must be non-negative")
        return value

    @field_validator("interval_seconds", "github_timeout_seconds")
    @classmethod
    def validate_positive_seconds(cls, value: float) -> float:
        """Validate recurring interval and network timeout durations."""
        if value <= 0:
            raise ValueError("duration must be positive")
        return value
