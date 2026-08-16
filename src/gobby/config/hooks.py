"""Hook timeout and additionalContext budget configuration."""

from pydantic import BaseModel, Field, field_validator


class HookTimeoutConfig(BaseModel):
    """Timeouts and additionalContext budgets for hook execution."""

    adapter_timeout: float = Field(
        default=105.0,
        gt=0,
        description="Daemon endpoint timeout in seconds for synchronous hook execution.",
    )
    provider_timeout: int = Field(
        default=120,
        gt=0,
        description="Provider hook timeout in seconds emitted by supported installers.",
    )
    additional_context_limit: int = Field(
        default=9_950,
        ge=256,
        description=(
            "Default additionalContext character limit for hook injection. "
            "Claude Code / Agent SDK hard-truncates at 10K; keep the default "
            "slightly below that. Raise a provider override when its CLI limit grows."
        ),
    )
    additional_context_limits: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-provider additionalContext character limits keyed by session "
            "source (claude, grok, codex, qwen, droid, agy). Unset providers "
            "use additional_context_limit."
        ),
    )

    @field_validator("additional_context_limits")
    @classmethod
    def normalize_additional_context_limits(cls, value: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for raw_key, raw_limit in value.items():
            key = raw_key.strip().casefold()
            if not key:
                raise ValueError("provider key must be non-empty")
            if raw_limit < 256:
                raise ValueError("additional context limit must be >= 256")
            normalized[key] = raw_limit
        return normalized
