"""Daemon-owned AI generation configuration."""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gobby._generated_tool_loop_limits import (
    DEFAULT_LOOP_TIMEOUT_SECONDS,
    DEFAULT_MAX_BYTES_PER_TOOL_RESULT,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TURNS,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
)
from gobby.config.feature_base import (
    FeatureCandidateConfig,
    FeatureProfile,
    validate_feature_candidates,
)
from gobby.config.url_validation import validate_endpoint_url

logger = logging.getLogger(__name__)

_GENERATION_ENDPOINT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
GenerationEndpointProtocol = Literal["openai-compatible", "lmstudio", "ollama", "vllm"]
GenerationWireAPI = Literal["chat-completions", "responses"]


class ToolLoopConfig(BaseModel):
    """Canonical ``ai.generation.tool_loop`` defaults and validation."""

    model_config = ConfigDict(extra="forbid")

    max_turns: int | None = Field(default=DEFAULT_MAX_TURNS, gt=0)
    max_tool_calls: int = Field(default=DEFAULT_MAX_TOOL_CALLS, gt=0)
    max_bytes_per_tool_result: int = Field(
        default=DEFAULT_MAX_BYTES_PER_TOOL_RESULT,
        gt=0,
    )
    tool_timeout_seconds: float = Field(default=DEFAULT_TOOL_TIMEOUT_SECONDS, gt=0)
    loop_timeout_seconds: int = Field(default=DEFAULT_LOOP_TIMEOUT_SECONDS, gt=0)


class GenerationEndpointConfig(BaseModel):
    """Text generation endpoint profile."""

    model_config = ConfigDict(extra="forbid")

    protocol: GenerationEndpointProtocol = Field(
        default="openai-compatible",
        description="Endpoint protocol adapter: openai-compatible, lmstudio, ollama, or vllm.",
    )
    wire_api: GenerationWireAPI = Field(
        default="chat-completions",
        description="Wire contract used by the endpoint.",
    )
    api_base: str = Field(
        description="Endpoint API base URL, for example https://openrouter.ai/api/v1.",
    )
    model: str = Field(
        description="Default model name for this generation endpoint.",
    )
    api_key: str | None = Field(
        default=None,
        repr=False,
        description="API key for the endpoint. Use $secret:NAME for encrypted storage.",
    )
    tool_chat: bool = Field(
        default=False,
        description="Whether this endpoint's model supports daemon tool_chat requests.",
    )
    probed_model: str | None = Field(
        default=None,
        description="Resolved served-model id from the last activation probe.",
    )
    input_modalities: list[str] | None = Field(
        default=None,
        description="Activation-owned modalities for probed_model; None means unknown.",
    )
    probed_json: bool | None = Field(
        default=None,
        description="Whether the last activation JSON-mode probe succeeded.",
    )
    probed_tools: bool | None = Field(
        default=None,
        description=(
            "Whether the last activation tool-call probe succeeded. None when skipped "
            "because tool_chat is false."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def strip_removed_vision_extract(cls, value: Any) -> Any:
        if isinstance(value, dict) and "vision_extract" in value:
            stripped = dict(value)
            stripped.pop("vision_extract")
            return stripped
        return value

    @model_validator(mode="after")
    def validate_endpoint(self) -> GenerationEndpointConfig:
        """Require endpoint URL and model names to be non-empty."""
        if not self.api_base.strip():
            raise ValueError("api_base must be set for generation endpoints")
        if not self.model.strip():
            raise ValueError("model must be set for generation endpoints")
        if self.wire_api == "responses" and self.protocol != "openai-compatible":
            raise ValueError("wire_api='responses' requires protocol='openai-compatible'")
        return self

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: str) -> str:
        return validate_endpoint_url(value, field_name="api_base")


class GenerationConfig(BaseModel):
    """Text generation settings independent from embedding settings."""

    model_config = ConfigDict(extra="forbid")

    tool_loop: ToolLoopConfig = Field(
        default_factory=ToolLoopConfig,
        description="Shared limits for daemon and standalone tool-calling loops.",
    )
    timeout_seconds: float = Field(
        default=1200.0,
        gt=0.0,
        description="Maximum seconds to wait for a daemon-owned text generation attempt.",
    )
    candidate_timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        description=(
            "Maximum seconds for a single text-generation candidate attempt on a "
            "fast (local/OpenAI-compatible API) lane before falling back to the "
            "next candidate."
        ),
    )
    cli_candidate_timeout_seconds: float = Field(
        default=600.0,
        gt=0.0,
        description=(
            "Maximum seconds for a single candidate attempt on a spawn-cold lane "
            "(CLI subprocess, daemon, ACP, or the Claude SDK that spawns a CLI). "
            "These lanes pay cold-start cost, so they get more headroom than the "
            "fast-lane candidate_timeout_seconds. Must stay at or below timeout_seconds."
        ),
    )
    spawn_cold_max_concurrency: int = Field(
        default=3,
        ge=1,
        description=("Host-wide maximum concurrent text-generation attempts on spawn-cold lanes."),
    )
    endpoints: dict[str, GenerationEndpointConfig] = Field(
        default_factory=dict,
        description="Named generation endpoints keyed by lowercase slug.",
    )
    profile_defaults: dict[FeatureProfile, list[FeatureCandidateConfig]] = Field(
        default_factory=dict,
        description="Profile default candidate overrides keyed by feature profile.",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_removed_local_config(cls, value: Any) -> Any:
        if isinstance(value, dict) and "local" in value:
            raise ValueError(
                "ai.generation.local.* has been removed; replace it with ai.generation.endpoints.*"
            )
        return value

    @field_validator("endpoints")
    @classmethod
    def validate_endpoint_names(
        cls,
        endpoints: dict[str, GenerationEndpointConfig],
    ) -> dict[str, GenerationEndpointConfig]:
        """Require endpoint aliases to be stable provider suffixes."""
        invalid = [name for name in endpoints if not _GENERATION_ENDPOINT_NAME_RE.fullmatch(name)]
        if invalid:
            joined = ", ".join(repr(name) for name in invalid)
            raise ValueError(f"generation endpoint names must match [a-z0-9][a-z0-9_-]*: {joined}")
        return endpoints

    @model_validator(mode="after")
    def clamp_candidate_timeouts(self) -> GenerationConfig:
        """Keep cli_candidate_timeout_seconds at or below the overall attempt budget.

        A per-candidate cap longer than the overall ``timeout_seconds`` attempt
        budget is meaningless, so clamp (with a warning) instead of failing — this
        keeps small-timeout test/dev configs valid.
        """
        for field_name in ("candidate_timeout_seconds", "cli_candidate_timeout_seconds"):
            candidate_timeout = getattr(self, field_name)
            if candidate_timeout <= self.timeout_seconds:
                continue
            logger.warning(
                "%s (%.3g) exceeds timeout_seconds (%.3g); clamping to timeout_seconds",
                field_name,
                candidate_timeout,
                self.timeout_seconds,
            )
            setattr(self, field_name, self.timeout_seconds)
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


class ModelMetadataAlias(BaseModel):
    """Provider-scoped identity correction for OpenRouter model metadata."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    provider_model_id: str
    openrouter_model_id: str

    @field_validator("provider", "provider_model_id")
    @classmethod
    def normalize_source_key(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("model metadata alias source fields must not be blank")
        return normalized

    @field_validator("openrouter_model_id")
    @classmethod
    def validate_target(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model metadata alias target must not be blank")
        return normalized


def model_metadata_alias_source_key(provider: str, provider_model_id: str) -> tuple[str, str]:
    """Return the normalized provider-scoped alias source key."""
    return provider.strip().casefold(), provider_model_id.strip().casefold()


def default_model_metadata_aliases() -> list[ModelMetadataAlias]:
    """Return bundled provider aliases as typed configuration defaults."""
    return [
        ModelMetadataAlias(
            provider="droid",
            provider_model_id="claude-haiku-4-5-20251001",
            openrouter_model_id="anthropic/claude-haiku-4.5",
        ),
        ModelMetadataAlias(
            provider="droid",
            provider_model_id="deepseek-v4-pro",
            openrouter_model_id="deepseek/deepseek-v4-pro",
        ),
        ModelMetadataAlias(
            provider="droid",
            provider_model_id="grok-4.5",
            openrouter_model_id="x-ai/grok-4.5",
        ),
        ModelMetadataAlias(
            provider="droid",
            provider_model_id="nemotron-3-ultra",
            openrouter_model_id="nvidia/nemotron-3-ultra-550b-a55b",
        ),
    ]


class AIConfig(BaseModel):
    """Daemon-owned AI configuration namespace."""

    model_config = ConfigDict(extra="forbid")

    generation: GenerationConfig = Field(
        default_factory=GenerationConfig,
        description="Text generation configuration.",
    )
    model_metadata_aliases: list[ModelMetadataAlias] = Field(
        default_factory=default_model_metadata_aliases,
        description="Provider-scoped aliases into the OpenRouter model metadata registry.",
    )

    @model_validator(mode="after")
    def validate_model_metadata_aliases(self) -> AIConfig:
        keys: set[tuple[str, str]] = set()
        for alias in self.model_metadata_aliases:
            key = (alias.provider, alias.provider_model_id)
            if key in keys:
                raise ValueError(
                    "duplicate model metadata alias source: "
                    f"{alias.provider}/{alias.provider_model_id}"
                )
            keys.add(key)
        return self


def parse_model_metadata_aliases(value: object | None) -> tuple[ModelMetadataAlias, ...]:
    """Validate a ConfigStore alias value using the daemon AI schema."""
    raw_value = [] if value is None else value
    config = AIConfig.model_validate({"model_metadata_aliases": raw_value})
    return tuple(config.model_metadata_aliases)
