"""
LLM providers configuration module.

Contains LLM-related Pydantic config models:
- LLMProviderConfig: Single provider config (models, auth_mode)
- LLMProvidersConfig: Multi-provider config (claude, codex, gemini)

Extracted from app.py using Strangler Fig pattern for code decomposition.
"""

from typing import Literal

from pydantic import BaseModel, Field

__all__ = ["LLMProviderConfig", "LLMProvidersConfig"]


class LLMProviderConfig(BaseModel):
    """Configuration for a single LLM provider."""

    models: str = Field(
        description="Comma-separated list of available models for this provider",
    )
    default_model: str | None = Field(
        default=None,
        description="Default model for this provider when callers don't specify one. "
        "Falls back to LLMProvidersConfig.default_model if not set.",
    )
    auth_mode: Literal["subscription", "api_key", "adc"] = Field(
        default="subscription",
        description="Authentication mode: 'subscription' (CLI-based), 'api_key' (BYOK), 'adc' (Google ADC)",
    )

    def get_models_list(self) -> list[str]:
        """Return models as a list."""
        return [m.strip() for m in self.models.split(",") if m.strip()]


class LLMProvidersConfig(BaseModel):
    """
    Configuration for multiple LLM providers.

    Example YAML:
    ```yaml
    llm_providers:
      json_strict: true  # Strict JSON validation for LLM responses (default)
      claude:
        models: haiku,sonnet,opus
      codex:
        models: gpt-5.4,gpt-5.4-mini,gpt-5.3-codex,gpt-5.3-codex-spark
        auth_mode: subscription
      gemini:
        models: gemini-3.1-pro-preview,gemini-3-flash-preview
        auth_mode: subscription
    ```
    """

    default_model: str | None = Field(
        default="opus",
        description="Default model for the web UI chat dropdown (e.g. 'opus', 'sonnet', 'haiku')",
    )
    json_strict: bool = Field(
        default=True,
        description="Strict JSON validation for LLM responses. "
        "When True (default), type mismatches raise errors. "
        "When False, allows coercion (e.g., '5' -> 5). "
        "Can be overridden per-workflow via llm_json_strict variable.",
    )
    claude: LLMProviderConfig | None = Field(
        default_factory=lambda: LLMProviderConfig(
            models="haiku,sonnet,opus",
            auth_mode="subscription",
        ),
        description="Claude provider configuration",
    )
    codex: LLMProviderConfig | None = Field(
        default=None,
        description="Codex (OpenAI) provider configuration",
    )
    gemini: LLMProviderConfig | None = Field(
        default=None,
        description="Gemini provider configuration",
    )

    def get_enabled_providers(self) -> list[str]:
        """Return list of enabled provider names."""
        providers = []
        if self.claude:
            providers.append("claude")
        if self.codex:
            providers.append("codex")
        if self.gemini:
            providers.append("gemini")
        return providers
