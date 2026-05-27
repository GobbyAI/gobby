"""Configuration for daemon-owned system loops."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class AutomationLoopConfig(BaseModel):
    """Configuration for the system automation loop."""

    enabled: bool = Field(
        default=True,
        description="Enable daemon-owned automation dispatch and maintenance.",
    )
    interval_seconds: int = Field(
        default=60,
        description="How often the automation loop runs, in seconds.",
    )

    @field_validator("interval_seconds")
    @classmethod
    def validate_interval(cls, value: int) -> int:
        if value < 1:
            raise ValueError("interval_seconds must be at least 1")
        return value


class SystemLoopsConfig(BaseModel):
    """Configuration for daemon-owned background system loops."""

    automation: AutomationLoopConfig = Field(
        default_factory=AutomationLoopConfig,
        description="Task dispatch and pipeline maintenance automation loop.",
    )
