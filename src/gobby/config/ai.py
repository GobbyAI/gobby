"""Daemon-owned AI generation configuration."""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gobby.config.feature_base import (
    FeatureCandidateConfig,
    FeatureProfile,
    validate_feature_candidates,
)

logger = logging.getLogger(__name__)

_LOCAL_ENDPOINT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
LocalGenerationProvider = Literal["openai-compatible", "lmstudio", "ollama"]


class LocalGenerationEndpointConfig(BaseModel):
    """Local text generation endpoint profile."""

    model_config = ConfigDict(extra="forbid")

    provider: LocalGenerationProvider = Field(
        default="openai-compatible",
        description="Local provider adapter: openai-compatible, lmstudio, or ollama.",
    )
    api_base: str = Field(
        description="Provider API base URL, for example http://localhost:1234.",
    )
    model: str = Field(
        description="Default model name for this local generation endpoint.",
    )
    api_key: str | None = Field(
        default=None,
        description="API key for the local endpoint. Use $secret:NAME for encrypted storage.",
    )
    vision_extract: bool = Field(
        default=False,
        description="Whether this endpoint's model supports daemon vision_extract requests.",
    )

    @model_validator(mode="after")
    def validate_endpoint(self) -> LocalGenerationEndpointConfig:
        """Require endpoint URL and model names to be non-empty."""
        if not self.api_base.strip():
            raise ValueError("api_base must be set for local generation endpoints")
        if not self.model.strip():
            raise ValueError("model must be set for local generation endpoints")
        return self


class LocalGenerationConfig(BaseModel):
    """Named local OpenAI-compatible text generation endpoints."""

    model_config = ConfigDict(extra="forbid")

    endpoints: dict[str, LocalGenerationEndpointConfig] = Field(
        default_factory=dict,
        description="Named local generation endpoints keyed by lowercase slug.",
    )

    @field_validator("endpoints")
    @classmethod
    def validate_endpoint_names(
        cls,
        endpoints: dict[str, LocalGenerationEndpointConfig],
    ) -> dict[str, LocalGenerationEndpointConfig]:
        """Require endpoint aliases to be stable provider suffixes."""
        invalid = [name for name in endpoints if not _LOCAL_ENDPOINT_NAME_RE.fullmatch(name)]
        if invalid:
            joined = ", ".join(repr(name) for name in invalid)
            raise ValueError(
                f"local generation endpoint names must match [a-z0-9][a-z0-9_-]*: {joined}"
            )
        return endpoints


class GenerationConfig(BaseModel):
    """Text generation settings independent from embedding settings."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(
        default=600.0,
        gt=0.0,
        description="Maximum seconds to wait for a daemon-owned text generation attempt.",
    )
    candidate_timeout_seconds: float = Field(
        default=60.0,
        gt=0.0,
        description=(
            "Maximum seconds for a single text-generation candidate attempt on a "
            "fast (local/OpenAI-compatible API) lane before falling back to the "
            "next candidate."
        ),
    )
    cli_candidate_timeout_seconds: float = Field(
        default=150.0,
        gt=0.0,
        description=(
            "Maximum seconds for a single candidate attempt on a spawn-cold lane "
            "(CLI subprocess, daemon, ACP, or the Claude SDK that spawns a CLI). "
            "These lanes pay cold-start cost, so they get more headroom than the "
            "fast-lane candidate_timeout_seconds. Must stay below timeout_seconds."
        ),
    )
    spawn_cold_max_concurrency: int = Field(
        default=3,
        ge=1,
        description=("Host-wide maximum concurrent text-generation attempts on spawn-cold lanes."),
    )
    local: LocalGenerationConfig = Field(
        default_factory=LocalGenerationConfig,
        description="Named local OpenAI-compatible generation endpoints.",
    )
    profile_defaults: dict[FeatureProfile, list[FeatureCandidateConfig]] = Field(
        default_factory=dict,
        description="Profile default candidate overrides keyed by feature profile.",
    )

    @model_validator(mode="after")
    def clamp_candidate_timeouts(self) -> GenerationConfig:
        """Keep cli_candidate_timeout_seconds at or below the overall attempt budget.

        A per-candidate cap longer than the overall ``timeout_seconds`` attempt
        budget is meaningless, so clamp (with a warning) instead of failing — this
        keeps small-timeout test/dev configs valid.
        """
        if self.cli_candidate_timeout_seconds > self.timeout_seconds:
            logger.warning(
                "cli_candidate_timeout_seconds (%.3g) exceeds timeout_seconds (%.3g); "
                "clamping to timeout_seconds",
                self.cli_candidate_timeout_seconds,
                self.timeout_seconds,
            )
            self.cli_candidate_timeout_seconds = self.timeout_seconds
        return self

    @field_validator("profile_defaults")
    @classmethod
    def validate_profile_defaults(
        cls,
        defaults: dict[FeatureProfile, list[FeatureCandidateConfig]],
    ) -> dict[FeatureProfile, list[FeatureCandidateConfig]]:
        """Validate global feature profile candidate overrides."""
        return {
            profile: validate_feature_candidates(candidates)
            for profile, candidates in defaults.items()
        }


class AIConfig(BaseModel):
    """Daemon-owned AI configuration namespace."""

    model_config = ConfigDict(extra="forbid")

    generation: GenerationConfig = Field(
        default_factory=GenerationConfig,
        description="Text generation configuration.",
    )
