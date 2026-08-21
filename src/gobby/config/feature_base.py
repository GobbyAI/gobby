"""Base configuration for LLM-backed feature routing."""

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

__all__ = [
    "DEFAULT_PROFILE_CANDIDATES",
    "FeatureCandidateConfig",
    "FeatureDefaultConfig",
    "FeatureProfile",
    "candidate_labels",
    "candidate_runtime_entries",
    "default_candidates_for_profile",
    "default_reasoning_for_profile",
    "iter_feature_default_configs",
    "normalize_feature_candidate",
    "parse_feature_candidate",
    "validate_feature_candidate",
    "validate_feature_candidates",
]


class FeatureProfile(StrEnum):
    """Provider-agnostic feature generation profiles."""

    LOW = "feature_low"
    MID = "feature_mid"
    HIGH = "feature_high"


def _parse_feature_candidate_label(candidate: str) -> tuple[str, str]:
    provider, separator, model = candidate.partition("/")
    if provider.strip().startswith("local:"):
        raise ValueError(
            f"{candidate!r} uses the removed local: selector; replace it with endpoint:*"
        )
    if not separator or not provider.strip() or not model.strip():
        raise ValueError(f"feature candidate must use provider/model format: {candidate!r}")
    return provider.strip(), model.strip()


def _normalize_reasoning_effort(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("reasoning_effort must be a string, 'auto', or null")
    normalized = value.strip().lower()
    if not normalized or normalized == "auto":
        return None
    return normalized


class FeatureCandidateConfig(BaseModel):
    """One provider/model candidate plus an optional reasoning effort pin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: str = Field(
        description="Provider/model candidate label, for example 'codex/gpt-5.6-terra'.",
    )
    reasoning_effort: str | None = Field(
        default=None,
        description="'auto'/unset or a provider-specific reasoning effort pin.",
    )

    @model_validator(mode="before")
    @classmethod
    def coerce_legacy_candidate(cls, value: Any) -> Any:
        """Allow legacy string candidates where structured entries are accepted."""
        if isinstance(value, str):
            return {"candidate": value}
        return value

    @field_validator("candidate")
    @classmethod
    def validate_candidate_label(cls, value: str) -> str:
        """Validate provider/model shape without checking model availability."""
        provider, model = _parse_feature_candidate_label(value)
        return f"{provider}/{model}"

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def normalize_reasoning_effort(cls, value: object) -> str | None:
        """Normalize auto/empty to unset and defer effort support checks to runtime."""
        return _normalize_reasoning_effort(value)


type FeatureCandidateInput = str | FeatureCandidateConfig | Mapping[str, object]


# Built-in profiles stay cloud-only. Local fallback candidates are explicit
# profile-default overrides using named local endpoints.
DEFAULT_PROFILE_CANDIDATES: dict[FeatureProfile, tuple[FeatureCandidateConfig, ...]] = {
    FeatureProfile.LOW: (
        FeatureCandidateConfig(candidate="codex/gpt-5.6-luna"),
        FeatureCandidateConfig(candidate="claude/haiku"),
    ),
    FeatureProfile.MID: (
        FeatureCandidateConfig(candidate="codex/gpt-5.6-terra"),
        FeatureCandidateConfig(candidate="claude/sonnet"),
    ),
    FeatureProfile.HIGH: (
        FeatureCandidateConfig(candidate="codex/gpt-5.6-sol", reasoning_effort="xhigh"),
        FeatureCandidateConfig(candidate="claude/opus", reasoning_effort="high"),
    ),
}

_DEFAULT_PROFILE_REASONING: dict[FeatureProfile, str | None] = {
    FeatureProfile.LOW: None,
    FeatureProfile.MID: None,
    FeatureProfile.HIGH: None,
}


_CLAUDE_FAMILY_ALIASES = ("haiku", "sonnet")


def default_candidates_for_profile(profile: FeatureProfile | str) -> tuple[str, ...]:
    """Return default provider/model candidate labels for a feature profile."""
    return candidate_labels(DEFAULT_PROFILE_CANDIDATES[FeatureProfile(profile)])


def default_reasoning_for_profile(profile: FeatureProfile | str) -> str | None:
    """Return the default reasoning effort for a feature profile."""
    return _DEFAULT_PROFILE_REASONING[FeatureProfile(profile)]


def _candidate_label(candidate: FeatureCandidateInput) -> str:
    if isinstance(candidate, FeatureCandidateConfig):
        return candidate.candidate
    if isinstance(candidate, Mapping):
        raw_candidate = candidate.get("candidate")
        return raw_candidate if isinstance(raw_candidate, str) else ""
    return candidate


def normalize_feature_candidate(candidate: FeatureCandidateInput) -> str:
    """Canonicalize provider-scoped feature candidate labels."""
    candidate_label = _candidate_label(candidate)
    try:
        provider, model = parse_feature_candidate(candidate_label)
    except ValueError:
        return candidate_label
    if provider != "claude":
        return candidate_label
    model_label = model.strip().lower()
    if model_label in _CLAUDE_FAMILY_ALIASES:
        return f"{provider}/{model_label}"
    if model_label.startswith(("claude-", "claude_")):
        for token in re.split(r"[-_]", model_label)[1:]:
            if token in _CLAUDE_FAMILY_ALIASES:
                return f"{provider}/{token}"
    return candidate_label


def parse_feature_candidate(candidate: FeatureCandidateInput) -> tuple[str, str]:
    """Return provider and model from a provider/model candidate label."""
    return _parse_feature_candidate_label(_candidate_label(candidate))


def _dedupe_normalized_candidates(
    candidates: list[FeatureCandidateConfig],
) -> list[FeatureCandidateConfig]:
    """Normalize candidates and preserve the first occurrence of each value."""
    seen: set[str] = set()
    normalized_candidates: list[FeatureCandidateConfig] = []
    for candidate in candidates:
        normalized = normalize_feature_candidate(candidate.candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_candidates.append(candidate.model_copy(update={"candidate": normalized}))
    return normalized_candidates


def validate_feature_candidate(candidate: FeatureCandidateInput) -> FeatureCandidateConfig:
    """Validate one feature candidate entry without checking runtime support."""
    return FeatureCandidateConfig.model_validate(candidate)


def validate_feature_candidates(
    candidates: Sequence[FeatureCandidateInput],
) -> list[FeatureCandidateConfig]:
    """Validate and deduplicate provider-scoped feature candidates."""
    invalid: list[FeatureCandidateInput] = []
    parsed: list[FeatureCandidateConfig] = []
    for candidate in candidates:
        try:
            parsed.append(validate_feature_candidate(candidate))
        except ValueError as exc:
            if "removed local: selector" in str(exc):
                raise
            invalid.append(candidate)
    if invalid:
        joined = ", ".join(repr(candidate) for candidate in invalid)
        raise ValueError(f"feature candidates must use provider/model format: {joined}")
    return _dedupe_normalized_candidates(parsed)


def candidate_labels(candidates: Sequence[FeatureCandidateInput]) -> tuple[str, ...]:
    """Return normalized provider/model labels for feature candidates."""
    return tuple(candidate.candidate for candidate in validate_feature_candidates(candidates))


def candidate_runtime_entries(
    candidates: Sequence[FeatureCandidateInput],
    *,
    profile: FeatureProfile | str | None = None,
) -> tuple[FeatureCandidateConfig, ...]:
    """Return normalized candidates with profile-default reasoning fallback applied."""
    default_reasoning = default_reasoning_for_profile(profile) if profile is not None else None
    return tuple(
        candidate.model_copy(
            update={
                "reasoning_effort": (
                    candidate.reasoning_effort
                    if candidate.reasoning_effort is not None
                    else default_reasoning
                )
            }
        )
        for candidate in validate_feature_candidates(candidates)
    )


class FeatureDefaultConfig(BaseModel):
    """Base config for LLM-backed features."""

    model_config = ConfigDict(extra="forbid")
    _candidates_omitted: bool = PrivateAttr(default=False)

    profile: FeatureProfile = Field(
        default=FeatureProfile.LOW,
        description="Provider-agnostic capability profile requested by this feature.",
    )
    candidates: Sequence[FeatureCandidateInput] = Field(
        default_factory=list,
        description=(
            "Ordered provider/model candidates with optional reasoning pins, for example "
            "[{'candidate': 'codex/gpt-5.6-sol', 'reasoning_effort': 'xhigh'}, "
            "{'candidate': 'claude/haiku'}]."
        ),
    )

    @model_validator(mode="after")
    def populate_and_validate_candidates(self) -> "FeatureDefaultConfig":
        """Fill profile defaults and validate provider-scoped candidate labels."""
        self._candidates_omitted = "candidates" not in self.model_fields_set
        if self._candidates_omitted:
            self.candidates = list(DEFAULT_PROFILE_CANDIDATES[FeatureProfile(self.profile)])
        self.candidates = validate_feature_candidates(self.candidates)
        if not self._candidates_omitted:
            static_default = validate_feature_candidates(
                list(DEFAULT_PROFILE_CANDIDATES[FeatureProfile(self.profile)])
            )
            if list(self.candidates) == static_default:
                # The values API refuses to store equal-to-default pins, so an
                # explicit list equal to the static profile default can only be
                # materialization residue (an exported baked default re-imported
                # as user config). Treat it as omitted so
                # ai.generation.profile_defaults keeps flowing through.
                self._candidates_omitted = True
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
