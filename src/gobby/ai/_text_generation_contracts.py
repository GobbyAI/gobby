"""Contracts shared by daemon text generation modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from gobby.config.feature_base import FeatureCandidateInput

if TYPE_CHECKING:
    from gobby.llm.base import LLMTextResult


@dataclass(frozen=True, kw_only=True)
class TextGenerationRequest:
    """One daemon text_generate request."""

    prompt: str
    provider: str | None = None
    profile: str | None = None
    candidates: tuple[FeatureCandidateInput, ...] = ()
    system_prompt: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    caller: str | None = None
    cwd: str | None = None
    candidate_timeout_seconds: float | None = None
    cli_candidate_timeout_seconds: float | None = None
    total_timeout_seconds: float | None = None
    images: list[str] | None = None
    json_schema: dict[str, Any] | None = field(default=None, compare=False, repr=False)
    output_validator: Callable[[str], str | None] | None = field(
        default=None,
        compare=False,
        repr=False,
    )


class TextGenerateAdapter(Protocol):
    """Adapter for one provider's text_generate execution path."""

    async def generate(self, request: TextGenerationRequest) -> str | LLMTextResult:
        """Generate text for the request."""


class TextGenerateJSONAdapter(Protocol):
    """Adapter with provider-native JSON generation support."""

    async def generate_json(self, request: TextGenerationRequest) -> dict[str, Any]:
        """Generate and parse JSON for the request."""


TextGenerateAdapterFactory = Callable[[], TextGenerateAdapter]


__all__ = [
    "TextGenerateAdapter",
    "TextGenerateAdapterFactory",
    "TextGenerateJSONAdapter",
    "TextGenerationRequest",
]
