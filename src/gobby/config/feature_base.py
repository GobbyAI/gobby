"""Base configuration for LLM-backed feature routing."""

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

__all__ = [
    "DEFAULT_PROFILE_CANDIDATES",
    "FeatureDefaultConfig",
    "FeatureProfile",
    "default_candidates_for_profile",
    "iter_feature_default_configs",
    "normalize_feature_candidate",
    "parse_feature_candidate",
    "validate_feature_candidates",
]


class FeatureProfile(StrEnum):
    """Provider-agnostic feature generation profiles."""

    LOW = "feature_low"
    MID = "feature_mid"
    HIGH = "feature_high"


# Built-in profiles stay cloud-only. Local fallback candidates are explicit
# profile-default overrides using named local endpoints.
DEFAULT_PROFILE_CANDIDATES: dict[FeatureProfile, tuple[str, ...]] = {
    FeatureProfile.LOW: (
        "codex/gpt-5.4-mini",
        "claude/haiku",
    ),
    FeatureProfile.MID: (
        "codex/gpt-5.4-mini",
        "claude/sonnet",
        "gemini/gemini-3.5-flash",
    ),
    FeatureProfile.HIGH: ("gemini/gemini-3.5-flash",),
}


_CLAUDE_FAMILY_ALIASES = ("haiku", "sonnet", "opus", "fable")


def default_candidates_for_profile(profile: FeatureProfile | str) -> tuple[str, ...]:
    """Return default provider/model candidates for a feature profile."""
    return DEFAULT_PROFILE_CANDIDATES[FeatureProfile(profile)]


def normalize_feature_candidate(candidate: str) -> str:
    """Canonicalize provider-scoped feature candidate labels."""
    try:
        provider, model = parse_feature_candidate(candidate)
    except ValueError:
        return candidate
    if provider != "claude":
        return candidate
    model_label = model.strip().lower()
    if model_label in _CLAUDE_FAMILY_ALIASES:
        return f"{provider}/{model_label}"
    if model_label.startswith(("claude-", "claude_")):
        for token in re.split(r"[-_]", model_label)[1:]:
            if token in _CLAUDE_FAMILY_ALIASES:
                return f"{provider}/{token}"
    return candidate


def parse_feature_candidate(candidate: str) -> tuple[str, str]:
    """Return provider and model from a provider/model candidate label."""
    provider, separator, model = candidate.partition("/")
    if not separator or not provider.strip() or not model.strip():
        raise ValueError(f"feature candidate must use provider/model format: {candidate!r}")
    return provider.strip(), model.strip()


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


def validate_feature_candidates(candidates: Sequence[str]) -> list[str]:
    """Validate and deduplicate provider-scoped feature candidates."""
    invalid = []
    for candidate in candidates:
        try:
            parse_feature_candidate(candidate)
        except ValueError:
            invalid.append(candidate)
    if invalid:
        joined = ", ".join(repr(candidate) for candidate in invalid)
        raise ValueError(f"feature candidates must use provider/model format: {joined}")
    return _dedupe_normalized_candidates(list(candidates))


class FeatureDefaultConfig(BaseModel):
    """Base config for LLM-backed features."""

    model_config = ConfigDict(extra="forbid")
    _candidates_omitted: bool = PrivateAttr(default=False)

    profile: FeatureProfile = Field(
        default=FeatureProfile.LOW,
        description="Provider-agnostic capability profile requested by this feature.",
    )
    candidates: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered provider/model candidates, for example "
            "['codex/gpt-5.4-mini', 'claude/haiku', "
            "'local:lm-studio/google/gemma-4-26b-a4b-qat']."
        ),
    )

    @model_validator(mode="after")
    def populate_and_validate_candidates(self) -> "FeatureDefaultConfig":
        """Fill profile defaults and validate provider-scoped candidate labels."""
        self._candidates_omitted = "candidates" not in self.model_fields_set
        if not self.candidates:
            self.candidates = list(default_candidates_for_profile(self.profile))
        self.candidates = validate_feature_candidates(self.candidates)
        return self


def iter_feature_default_configs(
    value: object,
    visited: set[int] | None = None,
) -> Iterable[FeatureDefaultConfig]:
    """Yield feature config models from nested config structures."""
    if visited is None:
        visited = set()
    if isinstance(value, (FeatureDefaultConfig, BaseModel, Mapping, Sequence)) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        object_id = id(value)
        if object_id in visited:
            return
        visited.add(object_id)
    if isinstance(value, FeatureDefaultConfig):
        yield value
        return
    if isinstance(value, BaseModel):
        for field_name in value.__class__.model_fields:
            yield from iter_feature_default_configs(getattr(value, field_name), visited)
        return
    if isinstance(value, Mapping):
        for item in value.values():
            yield from iter_feature_default_configs(item, visited)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from iter_feature_default_configs(item, visited)
