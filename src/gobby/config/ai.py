"""Daemon-owned AI generation configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LocalGenerationConfig(BaseModel):
    """OpenAI-compatible local text generation endpoint configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Enable local OpenAI-compatible text generation.",
    )
    api_base: str | None = Field(
        default=None,
        description="OpenAI-compatible API base URL, for example http://localhost:1234/v1.",
    )
    model: str | None = Field(
        default=None,
        description="Default local generation model.",
    )
    api_key: str | None = Field(
        default=None,
        description="API key for the local endpoint. Use $secret:NAME for encrypted storage.",
    )

    @model_validator(mode="after")
    def validate_enabled_endpoint(self) -> LocalGenerationConfig:
        """Require endpoint and model when local generation is enabled."""
        if self.enabled:
            if not self.api_base or not self.api_base.strip():
                raise ValueError("api_base must be set when local generation is enabled")
            if not self.model or not self.model.strip():
                raise ValueError("model must be set when local generation is enabled")
        return self


class GenerationConfig(BaseModel):
    """Text generation settings independent from embedding settings."""

    model_config = ConfigDict(extra="forbid")

    local: LocalGenerationConfig = Field(
        default_factory=LocalGenerationConfig,
        description="Local OpenAI-compatible generation endpoint.",
    )


class AIConfig(BaseModel):
    """Daemon-owned AI configuration namespace."""

    model_config = ConfigDict(extra="forbid")

    generation: GenerationConfig = Field(
        default_factory=GenerationConfig,
        description="Text generation configuration.",
    )
