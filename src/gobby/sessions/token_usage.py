"""Shared token-usage extraction helpers.

Leaf module so typed-JSON transcript parsers and live ``after_model`` hooks share
one de-overlapped, thinking-aware mapping. ``TokenUsage`` is imported lazily
inside the function (not at module load) so importing this helper from the hook
layer does not eagerly pull in the transcripts package.
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


def typed_json_token_usage(usage_data: Mapping[str, Any]) -> TokenUsage:
    """Build a de-overlapped, thinking-aware TokenUsage from typed-JSON usage metadata.

    ``promptTokenCount``/``input`` already include the cached portion, so the
    cached count is subtracted from input. Thinking tokens are folded into the
    output count. Tool tokens are excluded because they are a subset of input
    in these payload families.
    """
    from gobby.sessions.transcripts.base import TokenUsage

    prompt = _coerce_token_count(usage_data.get("promptTokenCount", usage_data.get("input")))
    cache_read = _coerce_token_count(
        usage_data.get("cachedContentTokenCount", usage_data.get("cached"))
    )
    if cache_read > prompt:
        logger.warning(
            "cachedContentTokenCount/cached exceeds promptTokenCount/input; anomalous usage data "
            "will be clamped when uncached input tokens are computed",
            extra={
                "prompt_tokens": prompt,
                "cache_read_tokens": cache_read,
                "action": "will_clamp_later",
            },
        )
    output = _coerce_token_count(
        usage_data.get("candidatesTokenCount", usage_data.get("output"))
    ) + _coerce_token_count(
        usage_data.get("thoughtsTokenCount")
        or usage_data.get("thinkingTokenCount")
        or usage_data.get("thoughts")
    )
    return TokenUsage(
        input_tokens=max(0, prompt - cache_read),
        output_tokens=output,
        cache_read_tokens=cache_read,
    )
