"""Shared LLM result and error primitives."""

from dataclasses import dataclass
from typing import Literal

# Auth mode type for providers
AuthMode = Literal["subscription", "api_key", "adc"]


@dataclass(frozen=True, kw_only=True)
class LLMTextResult:
    """Generated text plus optional provider usage accounting."""

    text: str
    usage: dict[str, int] | None = None
    provider: str | None = None
    model: str | None = None
    profile: str | None = None
    applied_reasoning_effort: str | None = None


class LLMProviderCancellation(RuntimeError):
    """Raised when an LLM provider operation is cancelled by shutdown/termination."""


class VisionInputError(ValueError):
    """Raised when an image input is missing or unreadable."""


class VisionProviderError(RuntimeError):
    """Raised when a vision provider cannot produce a description."""


class VisionProviderUnavailableError(VisionProviderError):
    """Raised when a configured vision provider cannot be invoked."""


_VISION_ERROR_SENTINEL_PREFIXES = (
    "Image description unavailable",
    "Image description failed",
    "Image not found",
    "Failed to read image",
)


def validate_vision_description(text: str) -> str:
    """Reject empty or legacy provider error strings at success boundaries."""
    if not text.strip():
        raise VisionProviderError("Vision provider returned no description")
    if text.startswith(_VISION_ERROR_SENTINEL_PREFIXES):
        raise VisionProviderError("Vision provider returned an error sentinel")
    return text
