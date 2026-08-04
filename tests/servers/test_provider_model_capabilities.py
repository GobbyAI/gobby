"""Tests for provider/model capability records."""

from dataclasses import FrozenInstanceError

import pytest

from gobby.servers.provider_model_capabilities import (
    ProviderModelCapability,
    SpeedTier,
    build_capability_matrix,
)
from gobby.servers.provider_model_defaults import DROID_MODEL_CATALOG
from gobby.storage.model_metadata import ModelMetadata


DROID_FAST_VARIANTS = [
    ("gpt-5.5-fast", "gpt-5.5", 5.0),
    ("claude-opus-5-fast", "claude-opus-5", 4.0),
    ("gpt-5.3-codex-fast", "gpt-5.3-codex", 1.4),
]


def test_capability_defaults_to_standard_unknown_metadata() -> None:
    capability = ProviderModelCapability(provider="codex", model_id="gpt-5.3-codex")

    assert capability.supported_reasoning_efforts == ()
    assert capability.context_limit is None
    assert capability.speed_tier is SpeedTier.STANDARD
    assert capability.speed_multiplier is None


@pytest.mark.parametrize(
    ("multiplier", "expected"),
    [
        (None, SpeedTier.STANDARD),
        (1.0, SpeedTier.STANDARD),
        (1.4, SpeedTier.FAST),
        (5.0, SpeedTier.FAST),
    ],
)
def test_speed_tier_is_derived_from_multiplier(
    multiplier: float | None,
    expected: SpeedTier,
) -> None:
    assert SpeedTier.from_multiplier(multiplier) is expected


def test_capability_serialization_round_trip() -> None:
    capability = ProviderModelCapability(
        provider="droid",
        model_id="gpt-5.5-fast",
        supported_reasoning_efforts=("low", "medium", "high"),
        context_limit=400_000,
        speed_tier=SpeedTier.FAST,
        speed_multiplier=5.0,
    )

    serialized = capability.to_dict()

    assert serialized == {
        "provider": "droid",
        "model_id": "gpt-5.5-fast",
        "supported_reasoning_efforts": ["low", "medium", "high"],
        "context_limit": 400_000,
        "speed_tier": "fast",
        "speed_multiplier": 5.0,
    }
    assert ProviderModelCapability.from_row(serialized) == capability


def test_capability_is_frozen() -> None:
    capability = ProviderModelCapability(provider="claude", model_id="sonnet")

    with pytest.raises(FrozenInstanceError):
        capability.__setattr__("context_limit", 1)


def test_build_capability_matrix_uses_canonical_metadata_and_provider_reasoning() -> None:
    matrix = build_capability_matrix(
        model_metadata={
            "claude-sonnet-4-6": ModelMetadata(context_length=200_000),
        },
        provider_catalogs={
            "droid": [
                {
                    "value": "claude-sonnet-4-6",
                    "reasoning": {"supported_efforts": ["off", "high", "max"]},
                }
            ],
            "claude": [
                {
                    "value": "sonnet",
                    "canonical_id": "claude-sonnet-4-6",
                    "reasoning": {"supported_efforts": ["low", "medium", "high"]},
                }
            ],
        },
    )

    assert matrix[("claude", "claude-sonnet-4-6")].context_limit == 200_000
    assert matrix[("claude", "claude-sonnet-4-6")].supported_reasoning_efforts == (
        "low",
        "medium",
        "high",
    )
    assert matrix[("droid", "claude-sonnet-4-6")].context_limit == 200_000
    assert matrix[("droid", "claude-sonnet-4-6")].supported_reasoning_efforts == (
        "off",
        "high",
        "max",
    )


def test_build_capability_matrix_uses_catalog_membership_and_defers_agy() -> None:
    matrix = build_capability_matrix(
        model_metadata={
            "catalogued": ModelMetadata(context_length=128_000),
            "metadata-only": ModelMetadata(context_length=64_000),
            "agy-model": ModelMetadata(context_length=32_000),
        },
        provider_catalogs={
            "codex": [{"value": "catalogued"}, {"value": "catalog-only"}],
            "agy": [{"value": "agy-model"}],
        },
    )

    assert set(matrix) == {("codex", "catalog-only"), ("codex", "catalogued")}
    assert matrix[("codex", "catalogued")].context_limit == 128_000
    assert matrix[("codex", "catalog-only")].context_limit is None


def test_build_capability_matrix_has_deterministic_key_order() -> None:
    matrix = build_capability_matrix(
        model_metadata={},
        provider_catalogs={
            "droid": [{"value": "zeta"}, {"value": "alpha"}],
            "claude": [{"value": "alpha"}],
        },
    )

    assert list(matrix) == [
        ("claude", "alpha"),
        ("droid", "alpha"),
        ("droid", "zeta"),
    ]


@pytest.mark.parametrize(
    ("model_id", "base_model_id", "speed_multiplier"),
    DROID_FAST_VARIANTS,
)
def test_droid_fast_variants_declare_speed_metadata(
    model_id: str,
    base_model_id: str,
    speed_multiplier: float,
) -> None:
    models = {model["value"]: model for model in DROID_MODEL_CATALOG}

    assert models[model_id]["base_model_id"] == base_model_id
    assert models[model_id]["speed_multiplier"] == speed_multiplier


def test_build_capability_matrix_uses_explicit_fast_declarations() -> None:
    matrix = build_capability_matrix(
        model_metadata={},
        provider_catalogs={
            "droid": DROID_MODEL_CATALOG,
            "codex": [{"value": "gpt-5.5-fast"}],
        },
    )

    for model_id, base_model_id, speed_multiplier in DROID_FAST_VARIANTS:
        capability = matrix[("droid", model_id)]
        assert capability.speed_tier is SpeedTier.FAST
        assert capability.speed_multiplier == speed_multiplier

        base_capability = matrix[("droid", base_model_id)]
        assert base_capability.speed_tier is SpeedTier.STANDARD
        assert base_capability.speed_multiplier is None

    undeclared = matrix[("codex", "gpt-5.5-fast")]
    assert undeclared.speed_tier is SpeedTier.STANDARD
    assert undeclared.speed_multiplier is None
