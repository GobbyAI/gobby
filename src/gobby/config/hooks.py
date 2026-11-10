"""Hook timeout and additionalContext budget configuration."""

from pydantic import BaseModel, Field, field_validator

# Mirrors ``POST_TIMEOUT`` in ``crates/ghook/src/transport.rs``. This is the only
# hook deadline the daemon does not own: once it passes, ghook stops waiting and
# decides on its own, and no CLI treats a before-tool hook as critical, so the
# command runs entirely ungated. Every daemon-side hook timeout must therefore
# expire inside this window, or the daemon's own degradation never runs.
HOOK_TRANSPORT_WINDOW_SECONDS = 30.0


def _require_context_limit_floor(value: int) -> int:
    """Reject a limit below the minimum derived from the context label bound. Nothing is clamped."""
    # Imported here: the llm package imports this config module, so a top-level import cycles.
    from gobby.llm.sdk_utils import MAX_CONTEXT_LABEL_CHARS, minimum_additional_context_limit

    minimum = minimum_additional_context_limit()
    if value < minimum:
        raise ValueError(
            f"additional context limit {value} is below the minimum {minimum}, derived from "
            f"the {MAX_CONTEXT_LABEL_CHARS}-character context label bound"
        )
    return value


class HookTimeoutConfig(BaseModel):
    """Timeouts and additionalContext budgets for hook execution."""

    adapter_timeout: float = Field(
        default=26.0,
        gt=0,
        description=(
            "Daemon endpoint timeout in seconds for synchronous hook execution. "
            "Must expire inside the ghook transport window so the daemon answers "
            "before the client stops waiting."
        ),
    )
    provider_timeout: int = Field(
        default=120,
        gt=0,
        description="Provider hook timeout in seconds emitted by supported installers.",
    )
    additional_context_limit: int = Field(
        default=9_950,
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
            "source (claude, grok, codex, droid, agy). Unset providers "
            "use additional_context_limit."
        ),
    )

    @field_validator("additional_context_limit")
    @classmethod
    def require_context_limit_floor(cls, value: int) -> int:
        return _require_context_limit_floor(value)

    @field_validator("additional_context_limits")
    @classmethod
    def normalize_additional_context_limits(cls, value: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for raw_key, raw_limit in value.items():
            key = raw_key.strip().casefold()
            if not key:
                raise ValueError("provider key must be non-empty")
            _require_context_limit_floor(raw_limit)
            if key in normalized:
                raise ValueError(f"duplicate provider key after normalization: {key}")
            normalized[key] = raw_limit
        return normalized
