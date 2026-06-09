"""Base configuration for LLM-backed feature routing."""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "DEFAULT_PROFILE_CANDIDATES",
    "FeatureDefaultConfig",
    "FeatureProfile",
    "default_candidates_for_profile",
    "normalize_feature_candidate",
]


class FeatureProfile(StrEnum):
    """Provider-agnostic feature generation profiles."""

    LOW = "feature_low"
    MID = "feature_mid"
    HIGH = "feature_high"


DEFAULT_PROFILE_CANDIDATES: dict[FeatureProfile, tuple[str, ...]] = {
    FeatureProfile.LOW: (
        "codex/gpt-5.4-mini",
        "claude/haiku",
        "local/Qwen3-Coder-30B-A3B-Instruct",
    ),
    FeatureProfile.MID: (
        "codex/gpt-5.3-codex-spark",
        "claude/sonnet",
        "local/Qwen3-Coder-Next",
    ),
    FeatureProfile.HIGH: (
        "codex/gpt-5.3-codex",
        "claude/opus",
        "local/Qwen3-Coder-Next",
    ),
}


_CLAUDE_FAMILY_ALIASES = ("haiku", "sonnet", "opus")


def default_candidates_for_profile(profile: FeatureProfile | str) -> tuple[str, ...]:
    """Return default provider/model candidates for a feature profile."""
    return DEFAULT_PROFILE_CANDIDATES[FeatureProfile(profile)]


def normalize_feature_candidate(candidate: str) -> str:
    """Canonicalize provider-scoped feature candidate labels."""
    provider, separator, model = candidate.partition("/")
    if not separator or provider != "claude":
        return candidate
    model_label = model.strip().lower()
    if model_label in _CLAUDE_FAMILY_ALIASES:
        return f"{provider}/{model_label}"
    if model_label.startswith(("claude-", "claude_")):
        for token in re.split(r"[-_]", model_label)[1:]:
            if token in _CLAUDE_FAMILY_ALIASES:
                return f"{provider}/{token}"
    return candidate


def _dedupe_normalized_candidates(candidates: list[str]) -> list[str]:
    """Normalize candidates and preserve the first occurrence of each value."""
    seen: set[str] = set()
    normalized_candidates: list[str] = []
    for candidate in candidates:
        normalized = normalize_feature_candidate(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_candidates.append(normalized)
    return normalized_candidates


class FeatureDefaultConfig(BaseModel):
    """Base config for LLM-backed features."""

    model_config = ConfigDict(extra="forbid")

    profile: FeatureProfile = Field(
        default=FeatureProfile.LOW,
        description="Provider-agnostic capability profile requested by this feature.",
    )
    candidates: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered provider/model candidates, for example "
            "['codex/gpt-5.4-mini', 'local/Qwen3-Coder-30B-A3B-Instruct']."
        ),
    )

    @model_validator(mode="after")
    def populate_and_validate_candidates(self) -> "FeatureDefaultConfig":
        """Fill profile defaults and validate provider-scoped candidate labels."""
        if not self.candidates:
            self.candidates = list(default_candidates_for_profile(self.profile))
        invalid = []
        for candidate in self.candidates:
            provider, separator, model = candidate.partition("/")
            if not separator or not provider.strip() or not model.strip():
                invalid.append(candidate)
        if invalid:
            joined = ", ".join(repr(candidate) for candidate in invalid)
            raise ValueError(f"feature candidates must use provider/model format: {joined}")
        self.candidates = _dedupe_normalized_candidates(self.candidates)
        return self
