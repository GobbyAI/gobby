"""Generation capability binding configuration module.

This module keeps daemon-owned LLMProvider binding config. CLI/app-server
providers such as Codex own their authentication and model configuration.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["LLMProviderConfig", "LLMProvidersConfig"]


_OBSOLETE_CLI_PROVIDER_KEYS = frozenset({"codex", "gemini", "grok", "qwen"})


class LLMProviderConfig(BaseModel):
    """Configuration for one LLMProvider-backed generation capability binding."""

    model_config = ConfigDict(extra="forbid")

    models: str = Field(
        description="Comma-separated models exposed by this generation capability binding",
    )
    default_model: str | None = Field(
        default=None,
        description="Default model for this binding when callers do not specify one. "
        "Falls back to LLMProvidersConfig.default_model if not set.",
    )
    auth_mode: Literal["subscription", "api_key", "adc"] = Field(
        default="subscription",
        description=(
            "Authentication mode for the binding: 'subscription' (CLI-based), "
            "'api_key' (BYOK), 'adc' (Google ADC)"
        ),
    )

    def get_models_list(self) -> list[str]:
        """Return models as a list."""
        return [m.strip() for m in self.models.split(",") if m.strip()]


class LLMProvidersConfig(BaseModel):
    """Configuration for LLMProvider-backed generation capability bindings.

    Example YAML:
    ```yaml
    llm_providers:
      json_strict: true  # Strict JSON validation for LLM responses (default)
      claude:
        models: haiku,sonnet,opus
    ```
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def drop_obsolete_cli_provider_keys(cls, data: Any) -> Any:
        """Ignore removed CLI/app-server provider entries from older configs."""
        if not isinstance(data, dict):
            return data
        return {key: value for key, value in data.items() if key not in _OBSOLETE_CLI_PROVIDER_KEYS}

    default_model: str | None = Field(
        default="opus",
        description="Default model for LLMProvider-backed generation capability bindings.",
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
        description="Claude text_generate/vision_extract capability binding configuration",
    )

    def get_enabled_providers(self) -> list[str]:
        """Return list of enabled provider names."""
        providers = []
        if self.claude:
            providers.append("claude")
        return providers
