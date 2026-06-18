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
