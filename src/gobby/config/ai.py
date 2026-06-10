"""Daemon-owned AI generation configuration."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gobby.config.feature_base import FeatureProfile, validate_feature_candidates

_LOCAL_ENDPOINT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class LocalGenerationEndpointConfig(BaseModel):
    """OpenAI-compatible local text generation endpoint profile."""

    model_config = ConfigDict(extra="forbid")

    api_base: str = Field(
        description="OpenAI-compatible API base URL, for example http://localhost:1234/v1.",
    )
    model: str = Field(
        description="Default model name for this local generation endpoint.",
    )
    api_key: str | None = Field(
        default=None,
        description="API key for the local endpoint. Use $secret:NAME for encrypted storage.",
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

    local: LocalGenerationConfig = Field(
        default_factory=LocalGenerationConfig,
        description="Named local OpenAI-compatible generation endpoints.",
    )
    profile_defaults: dict[FeatureProfile, list[str]] = Field(
        default_factory=dict,
        description="Profile default candidate overrides keyed by feature profile.",
    )

    @field_validator("profile_defaults")
    @classmethod
    def validate_profile_defaults(
        cls,
        defaults: dict[FeatureProfile, list[str]],
    ) -> dict[FeatureProfile, list[str]]:
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
