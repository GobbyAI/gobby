"""Pipeline configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field

from gobby.config.feature_base import FeatureDefaultConfig


class PipelineConfig(BaseModel):
    """Configuration for pipeline execution."""

    prompt_step: FeatureDefaultConfig = Field(
        default_factory=FeatureDefaultConfig,
        description="LLM feature configuration for pipeline prompt steps.",
    )
    nesting_depth_limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum nesting depth for invoke_pipeline steps. "
        "Prevents stack overflow from recursive/circular pipelines.",
    )
