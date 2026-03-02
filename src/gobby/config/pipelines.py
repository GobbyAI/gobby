"""Pipeline configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PipelineConfig(BaseModel):
    """Configuration for pipeline execution."""

    nesting_depth_limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum nesting depth for invoke_pipeline steps. "
        "Prevents stack overflow from recursive/circular pipelines.",
    )
