"""Provider/model capability records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Self


class SpeedTier(str, Enum):
    """Execution speed tier for a provider model route."""

    STANDARD = "standard"
    FAST = "fast"

    @classmethod
    def from_multiplier(cls, multiplier: float | None) -> Self:
        """Derive a speed tier from an acceleration multiplier."""
        return cls.FAST if multiplier is not None and multiplier > 1 else cls.STANDARD


@dataclass(frozen=True)
class ProviderModelCapability:
    """Capabilities attached to a provider-specific model route."""

    provider: str
    model_id: str
    supported_reasoning_efforts: tuple[str, ...] = ()
    context_limit: int | None = None
    speed_tier: SpeedTier = SpeedTier.STANDARD
    speed_multiplier: float | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ProviderModelCapability:
        """Create a capability from a database-style row."""
        data = dict(row)
        provider = _required_string(data, "provider")
        model_id = _required_string(data, "model_id")
        reasoning_efforts = _reasoning_efforts(data.get("supported_reasoning_efforts"))

        context_limit = _optional_int(data.get("context_limit"), "context_limit")
        speed_multiplier = _optional_float(data.get("speed_multiplier"), "speed_multiplier")

        raw_speed_tier = data.get("speed_tier")
        if isinstance(raw_speed_tier, SpeedTier):
            speed_tier = raw_speed_tier
        elif raw_speed_tier is not None:
            speed_tier = SpeedTier(str(raw_speed_tier))
        else:
            speed_tier = SpeedTier.from_multiplier(speed_multiplier)

        return cls(
            provider=provider,
            model_id=model_id,
            supported_reasoning_efforts=reasoning_efforts,
            context_limit=context_limit,
            speed_tier=speed_tier,
            speed_multiplier=speed_multiplier,
        )

    def to_dict(self) -> dict[str, object]:
        """Convert the capability to a JSON-safe dictionary."""
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "supported_reasoning_efforts": list(self.supported_reasoning_efforts),
            "context_limit": self.context_limit,
            "speed_tier": self.speed_tier.value,
            "speed_multiplier": self.speed_multiplier,
        }


def _required_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _reasoning_efforts(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError("supported_reasoning_efforts must be a sequence of strings")
    if not all(isinstance(effort, str) for effort in value):
        raise ValueError("supported_reasoning_efforts must contain only strings")
    return tuple(value)


def _optional_int(value: object, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer or null")
    return value


def _optional_float(value: object, key: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key} must be numeric or null")
    return float(value)
