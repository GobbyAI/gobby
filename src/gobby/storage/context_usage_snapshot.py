"""Context usage snapshot tracking for normalized pressure decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from gobby.utils.datetime import normalize_datetime_model, utc_now

ContextUsageSource = Literal[
    "claude",
    "codex",
    "qwen",
    "droid",
    "agy",
    "grok",
    "web_chat",
]
ContextUsageConfidence = Literal["reported", "estimated", "unknown"]


@normalize_datetime_model(required=("timestamp",))
@dataclass(frozen=True)
class ContextUsageSnapshot:
    """Normalized context usage for current pressure decisions."""

    source: ContextUsageSource
    model: str | None
    context_window: int | None
    context_used_tokens: int | None
    context_usage_ratio: float | None
    confidence: ContextUsageConfidence
    timestamp: datetime
    raw_prompt_footprint: int | None = None
    uncached_prompt_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    output_tokens: int | None = None

    @classmethod
    def calculate_ratio(
        cls,
        context_used_tokens: int | None,
        context_window: int | None,
    ) -> float | None:
        """Calculate context usage ratio clamped to [0, 1]."""
        if context_used_tokens is None or context_window is None or context_window <= 0:
            return None
        ratio = context_used_tokens / context_window
        return max(0.0, min(1.0, ratio))

    @classmethod
    def window_only(
        cls,
        *,
        source: ContextUsageSource,
        context_window: int | None,
        model: str | None = None,
    ) -> ContextUsageSnapshot:
        """Create a snapshot when only model/window metadata is known."""
        return cls(
            source=source,
            model=model,
            context_window=context_window,
            context_used_tokens=None,
            context_usage_ratio=None,
            confidence="unknown",
            timestamp=utc_now(),
        )

    @classmethod
    def from_reported_occupancy(
        cls,
        *,
        source: ContextUsageSource,
        context_window: int | None,
        context_used_tokens: int | None,
        model: str | None = None,
    ) -> ContextUsageSnapshot:
        """Create a snapshot from provider-reported current-context occupancy."""
        context_used = _coerce_nonnegative_int(context_used_tokens)
        return cls(
            source=source,
            model=model,
            context_window=context_window,
            context_used_tokens=context_used,
            context_usage_ratio=cls.calculate_ratio(context_used, context_window),
            confidence="reported" if context_used is not None else "unknown",
            timestamp=utc_now(),
            raw_prompt_footprint=context_used,
        )

    @classmethod
    def from_token_breakdown(
        cls,
        *,
        source: ContextUsageSource,
        context_window: int | None,
        uncached_prompt_tokens: int | None,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        output_tokens: int | None = None,
        model: str | None = None,
        confidence: ContextUsageConfidence = "reported",
    ) -> ContextUsageSnapshot:
        """Create a provider snapshot from normalized per-turn token fields."""
        uncached = _coerce_nonnegative_int(uncached_prompt_tokens)
        cache_read = _coerce_nonnegative_int(cache_read_tokens)
        cache_creation = _coerce_nonnegative_int(cache_creation_tokens)
        output = _coerce_nonnegative_int(output_tokens)

        context_used = None
        if uncached is not None or cache_read is not None or cache_creation is not None:
            context_used = (uncached or 0) + (cache_read or 0) + (cache_creation or 0)

        ratio = cls.calculate_ratio(context_used, context_window)
        return cls(
            source=source,
            model=model,
            context_window=context_window,
            context_used_tokens=context_used,
            context_usage_ratio=ratio,
            confidence=confidence if context_used is not None else "unknown",
            timestamp=utc_now(),
            raw_prompt_footprint=context_used,
            uncached_prompt_tokens=uncached,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            output_tokens=output,
        )

    @classmethod
    def from_agy(
        cls,
        context_window: int | None,
        model: str | None = None,
    ) -> ContextUsageSnapshot:
        """Create snapshot from AGY provider.

        TODO(agy): AGY context-pressure support is incomplete until provider hooks or
        transcripts expose reliable per-turn usage. For v1, record window-only metadata.
        """
        return cls.window_only(source="agy", model=model, context_window=context_window)


def _coerce_nonnegative_int(value: Any) -> int | None:
    # bool is an int subclass; token counters must ignore true/false flags.
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
