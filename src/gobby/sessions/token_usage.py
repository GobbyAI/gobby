"""Shared token-usage extraction helpers.

Leaf module so both the Gemini transcript parser and the live ``after_model``
hook share one de-overlapped, thinking-aware mapping. ``TokenUsage`` is imported
lazily inside the function (not at module load) so importing this helper from
the hook layer does not eagerly pull in the transcripts package — whose
``__init__`` imports every parser, and whose ``gemini`` parser imports this
module right back (a cycle).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.sessions.transcripts.base import TokenUsage

logger = logging.getLogger(__name__)


def _coerce_token_count(value: Any) -> int:
    """Coerce a raw usage value to a non-negative int, treating bad input as 0."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def gemini_token_usage(usage_data: Mapping[str, Any]) -> TokenUsage:
    """Build a de-overlapped, thinking-aware TokenUsage from Gemini usage metadata.

    Gemini's ``promptTokenCount`` already includes the cached portion, so the
    uncached input is ``promptTokenCount - cachedContentTokenCount``. Thinking
    tokens (``thoughtsTokenCount``/``thinkingTokenCount``) are folded into the
    output count. ``toolUsePromptTokenCount`` is intentionally excluded — per
    the Gemini docs it is a subset of ``promptTokenCount``.
    """
    from gobby.sessions.transcripts.base import TokenUsage

    prompt = _coerce_token_count(usage_data.get("promptTokenCount"))
    cache_read = _coerce_token_count(usage_data.get("cachedContentTokenCount"))
    if cache_read > prompt:
        logger.warning(
            "Gemini cachedContentTokenCount exceeds promptTokenCount; clamping uncached input",
            extra={"prompt_tokens": prompt, "cache_read_tokens": cache_read},
        )
    output = _coerce_token_count(usage_data.get("candidatesTokenCount")) + _coerce_token_count(
        usage_data.get("thoughtsTokenCount") or usage_data.get("thinkingTokenCount")
    )
    return TokenUsage(
        input_tokens=max(0, prompt - cache_read),
        output_tokens=output,
        cache_read_tokens=cache_read,
    )
