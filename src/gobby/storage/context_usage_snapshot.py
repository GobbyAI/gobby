"""Context usage snapshot tracking for normalized pressure decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

ContextUsageSource = Literal[
    "claude",
    "codex",
    "gemini",
    "qwen",
    "droid",
    "agy",
    "grok",
    "web_chat",
]
ContextUsageConfidence = Literal["reported", "estimated", "unknown"]


@dataclass(frozen=True)
class ContextUsageSnapshot:
    """Normalized context usage for current pressure decisions."""

    source: ContextUsageSource
    model: str | None
    context_window: int | None
    context_used_tokens: int | None
    context_usage_ratio: float | None
    confidence: ContextUsageConfidence
    timestamp: str
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
            timestamp=_now_iso(),
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
            timestamp=_now_iso(),
            raw_prompt_footprint=context_used,
            uncached_prompt_tokens=uncached,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            output_tokens=output,
        )

    @classmethod
    def from_claude(
        cls,
        context_window: int | None,
        input_tokens: int | None,
        uncached_input_tokens: int | None,
        cache_read_tokens: int | None,
        cache_creation_tokens: int | None,
        output_tokens: int | None,
        model: str | None = None,
    ) -> ContextUsageSnapshot:
        """Create snapshot from Claude provider usage."""
        return cls.from_token_breakdown(
            source="claude",
            model=model,
            context_window=context_window,
            uncached_prompt_tokens=(
                uncached_input_tokens if uncached_input_tokens is not None else input_tokens
            ),
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            output_tokens=output_tokens,
        )

    @classmethod
    def from_codex(
        cls,
        context_window: int | None,
        last_token_usage: dict[str, Any] | None,
        total_token_usage: dict[str, Any] | None,
        char_fallback: str | None,
        model: str | None = None,
    ) -> ContextUsageSnapshot:
        """Create snapshot from Codex provider usage."""
        confidence: ContextUsageConfidence = "unknown"
        input_tokens: int | None = None
        output_tokens: int | None = None
        cache_read_tokens: int | None = None
        cache_creation_tokens: int | None = None
        context_used: int | None = None

        usage_data = last_token_usage or total_token_usage
        if usage_data:
            input_tokens = _first_token_count(
                usage_data,
                "input_tokens",
                "inputTokens",
                "prompt_tokens",
                "promptTokens",
            )
            output_tokens = _first_token_count(
                usage_data,
                "output_tokens",
                "outputTokens",
                "completion_tokens",
                "completionTokens",
            )
            reasoning_tokens = _first_token_count(
                usage_data,
                "reasoning_output_tokens",
                "reasoningOutputTokens",
                "reasoning_tokens",
                "reasoningTokens",
            )
            cache_read_tokens = _first_token_count(
                usage_data,
                "cached_input_tokens",
                "cachedInputTokens",
                "cache_read_input_tokens",
                "cacheReadInputTokens",
            )
            cache_creation_tokens = _first_token_count(
                usage_data,
                "cache_creation_input_tokens",
                "cacheCreationInputTokens",
            )
            if input_tokens is not None:
                context_used = input_tokens
                if cache_read_tokens is not None or cache_creation_tokens is not None:
                    input_tokens = max(
                        0,
                        input_tokens - (cache_read_tokens or 0) - (cache_creation_tokens or 0),
                    )
                output_tokens = (output_tokens or 0) + (reasoning_tokens or 0)
                confidence = "reported"

        if context_used is None and char_fallback:
            context_used = max(0, int(len(char_fallback) / 4))
            input_tokens = context_used
            confidence = "estimated"

        ratio = cls.calculate_ratio(context_used, context_window)
        return cls(
            source="codex",
            model=model,
            context_window=context_window,
            context_used_tokens=context_used,
            context_usage_ratio=ratio,
            confidence=confidence,
            timestamp=_now_iso(),
            raw_prompt_footprint=context_used,
            uncached_prompt_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            output_tokens=output_tokens,
        )

    @classmethod
    def from_gemini(
        cls,
        context_window: int | None,
        prompt_tokens: int | None,
        cached_content_tokens: int | None,
        output_tokens: int | None,
        reasoning_tokens: int | None,
        model: str | None = None,
    ) -> ContextUsageSnapshot:
        """Create snapshot from Gemini provider usage."""
        cache_read_tokens = _coerce_nonnegative_int(cached_content_tokens)
        uncached_prompt_tokens = _coerce_nonnegative_int(prompt_tokens)
        if uncached_prompt_tokens is not None and cache_read_tokens is not None:
            uncached_prompt_tokens = max(0, uncached_prompt_tokens - cache_read_tokens)
        return cls.from_token_breakdown(
            source="gemini",
            model=model,
            context_window=context_window,
            uncached_prompt_tokens=uncached_prompt_tokens,
            cache_read_tokens=cache_read_tokens,
            output_tokens=(output_tokens or 0) + (reasoning_tokens or 0),
        )

    @classmethod
    def from_qwen(
        cls,
        context_window: int | None,
        prompt_tokens: int | None,
        cached_content_tokens: int | None,
        output_tokens: int | None,
        model: str | None = None,
    ) -> ContextUsageSnapshot:
        """Create snapshot from Qwen provider usage (Gemini-compatible)."""
        cache_read_tokens = _coerce_nonnegative_int(cached_content_tokens)
        uncached_prompt_tokens = _coerce_nonnegative_int(prompt_tokens)
        if uncached_prompt_tokens is not None and cache_read_tokens is not None:
            uncached_prompt_tokens = max(0, uncached_prompt_tokens - cache_read_tokens)
        return cls.from_token_breakdown(
            source="qwen",
            model=model,
            context_window=context_window,
            uncached_prompt_tokens=uncached_prompt_tokens,
            cache_read_tokens=cache_read_tokens,
            output_tokens=output_tokens,
        )

    @classmethod
    def from_droid(
        cls,
        context_window: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        model: str | None = None,
    ) -> ContextUsageSnapshot:
        """Create snapshot from Droid provider usage."""
        return cls.from_token_breakdown(
            source="droid",
            model=model,
            context_window=context_window,
            uncached_prompt_tokens=input_tokens,
            output_tokens=output_tokens,
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

    @classmethod
    def from_grok(
        cls,
        context_window: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        model: str | None = None,
    ) -> ContextUsageSnapshot:
        """Create snapshot from Grok provider."""
        return cls.from_token_breakdown(
            source="grok",
            model=model,
            context_window=context_window,
            uncached_prompt_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    @classmethod
    def from_web_chat(
        cls,
        context_window: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        model: str | None = None,
    ) -> ContextUsageSnapshot:
        """Create snapshot from web chat provider."""
        return cls.from_token_breakdown(
            source="web_chat",
            model=model,
            context_window=context_window,
            uncached_prompt_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _now_iso() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _coerce_nonnegative_int(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, int(value))


def _first_token_count(data: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = data.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None
