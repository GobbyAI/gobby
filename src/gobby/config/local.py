"""
Local model endpoint configuration.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

__all__ = ["LocalConfig", "LocalLLMConfig"]


class LocalConfig(BaseModel):
    """Configuration for local model endpoint (e.g., LMStudio)."""

    url: str = Field(
        description="Local model API endpoint (e.g., http://localhost:1234/v1)",
    )
    model: str = Field(
        description="Model name to load/use at the local endpoint",
    )
    api_key: str | None = Field(
        default=None,
        description="API key for the local endpoint. Use $secret:NAME for encrypted secrets store.",
    )


class LocalLLMConfig(BaseModel):
    """Configuration for routing providers through a local LLM endpoint.

    When enabled, sets ANTHROPIC_BASE_URL for the specified providers so that
    Claude Code (or other tools) routes API traffic through the local endpoint
    (e.g., LMStudio, Ollama, llama.cpp server).
    """

    enabled: bool = Field(default=False, description="Enable local LLM endpoint override")
    endpoint: str = Field(
        default="",
        description="Base URL for the local LLM (e.g., http://localhost:1234/v1)",
    )
    providers: list[str] = Field(
        default_factory=lambda: ["claude"],
        description="Providers to route through the local endpoint",
    )

    @model_validator(mode="after")
    def validate_enabled_endpoint(self) -> LocalLLMConfig:
        """Require a non-empty endpoint when local routing is enabled."""
        if self.enabled and not self.endpoint.strip():
            raise ValueError("endpoint must be set when enabled is True")
        return self
