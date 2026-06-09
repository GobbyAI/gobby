"""Base configuration for LLM-backed feature routing."""

import re
from enum import StrEnum
from typing import Any

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
        "codex/gpt-5.3-codex-spark",
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
_LEGACY_TIER_PROFILE_ALIASES = {
    "low": FeatureProfile.LOW,
    "fast": FeatureProfile.LOW,
    "haiku": FeatureProfile.LOW,
    "mid": FeatureProfile.MID,
    "medium": FeatureProfile.MID,
    "sonnet": FeatureProfile.MID,
    "high": FeatureProfile.HIGH,
    "opus": FeatureProfile.HIGH,
}


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


def _legacy_profile(value: Any) -> Any:
    if isinstance(value, FeatureProfile):
        return value
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        return FeatureProfile(stripped)
    except ValueError:
        return _LEGACY_TIER_PROFILE_ALIASES.get(stripped.lower(), value)


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
            "['codex/gpt-5.3-codex-spark', 'local/Qwen3-Coder-30B-A3B-Instruct']."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_fields(cls, data: Any) -> Any:
        """Translate legacy provider/model/tier config before extras are rejected."""
        if not isinstance(data, dict):
            return data
        values = dict(data)
        provider = values.pop("provider", None)
        model = values.pop("model", None)
        tier = values.pop("tier", None)
        if (provider is None) != (model is None):
            raise ValueError("legacy provider and model must be specified together")
        if provider is not None and model is not None:
            legacy_candidate = normalize_feature_candidate(f"{provider}/{model}")
            raw_candidates = values.get("candidates")
            existing = (
                [
                    normalize_feature_candidate(str(candidate))
                    for candidate in raw_candidates
                    if isinstance(candidate, str)
                ]
                if isinstance(raw_candidates, (list, tuple))
                else []
            )
            values["candidates"] = _dedupe_normalized_candidates([*existing, legacy_candidate])
        if tier is not None and "profile" not in values:
            values["profile"] = _legacy_profile(tier)
        return values

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
