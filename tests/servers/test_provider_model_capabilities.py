"""Tests for provider/model capability records."""

from dataclasses import FrozenInstanceError

import pytest

from gobby.servers.provider_model_capabilities import ProviderModelCapability, SpeedTier


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
