"""
Base configuration for LLM-backed features.

Provides FeatureDefaultConfig — a shared base class with provider, model,
and tier fields — plus ModelTier enum and TIER_FALLBACK_MODEL mapping for
graceful degradation when the local provider is unavailable.
"""

from enum import Enum

from pydantic import BaseModel, Field

__all__ = [
    "FeatureDefaultConfig",
    "ModelTier",
    "TIER_FALLBACK_MODEL",
]


class ModelTier(str, Enum):
    """Complexity tier for LLM feature routing.

    Determines which Claude model to fall back to when a local provider
    is unavailable or fails.
    """

    LOW = "low"  # haiku — fast/cheap (title synthesis, tool summarization)
    MID = "mid"  # sonnet — moderate (session summaries, merge resolution)
    HIGH = "high"  # opus — heavy (code review, chat)


TIER_FALLBACK_MODEL: dict[ModelTier, str] = {
    ModelTier.LOW: "haiku",
    ModelTier.MID: "sonnet",
    ModelTier.HIGH: "opus",
}


class FeatureDefaultConfig(BaseModel):
    """Base config for LLM-backed features.

    Provides standardized provider/model/tier fields so that every feature
    config gets tier-based fallback for free when using the local provider.

    Subclasses override defaults as needed::

        class SessionSummaryConfig(FeatureDefaultConfig):
            model: str = Field(default="sonnet", ...)
            tier: ModelTier = Field(default=ModelTier.MID, ...)
    """

    provider: str = Field(
        default="claude",
        description="LLM provider to use (claude, codex, local)",
    )
    model: str = Field(
        default="haiku",
        description="Model name to use for this feature",
    )
    tier: ModelTier = Field(
        default=ModelTier.LOW,
        description="Complexity tier — determines fallback model when local provider fails",
    )
