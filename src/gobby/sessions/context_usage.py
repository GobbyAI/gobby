"""Helpers for session-level context usage snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from gobby.servers.provider_models import context_length_for_model
from gobby.storage.context_usage_snapshot import ContextUsageSnapshot, ContextUsageSource

if TYPE_CHECKING:
    from gobby.sessions.transcripts.base import TokenUsage

_SOURCES: frozenset[str] = frozenset(
    {"claude", "codex", "gemini", "qwen", "droid", "agy", "grok", "web_chat"}
)


def normalize_context_usage_source(source: str | None) -> ContextUsageSource | None:
    if not source:
        return None
    normalized = source.strip().lower().replace("-", "_")
    if normalized == "claude_code":
        normalized = "claude"
    if normalized in _SOURCES:
        return cast(ContextUsageSource, normalized)
    return None


def snapshot_from_token_usage(
    *,
    source: str | None,
    context_window: int | None,
    usage: TokenUsage,
    model: str | None,
) -> ContextUsageSnapshot | None:
    """Build a normalized snapshot from parser token usage."""
    snapshot_source = normalize_context_usage_source(source)
    if snapshot_source is None:
        return None
    resolved_window = context_window or context_window_for_source_model(snapshot_source, model)
    return ContextUsageSnapshot.from_token_breakdown(
        source=snapshot_source,
        context_window=resolved_window,
        uncached_prompt_tokens=usage.input_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
        output_tokens=usage.output_tokens,
        model=model,
    )


def snapshot_from_window_metadata(
    *,
    source: str | None,
    context_window: int | None,
    model: str | None,
) -> ContextUsageSnapshot | None:
    """Build a window-only snapshot when a provider omits per-turn usage."""
    snapshot_source = normalize_context_usage_source(source)
    if snapshot_source is None:
        return None
    resolved_window = context_window or context_window_for_source_model(snapshot_source, model)
    if resolved_window is None:
        return None
    if snapshot_source == "agy":
        return ContextUsageSnapshot.from_agy(context_window=resolved_window, model=model)
    return ContextUsageSnapshot.window_only(
        source=snapshot_source,
        context_window=resolved_window,
        model=model,
    )


def context_window_for_source_model(
    source: ContextUsageSource | str | None,
    model: str | None,
) -> int | None:
    """Resolve static context-window metadata for a provider/model pair."""
    snapshot_source = normalize_context_usage_source(source) if isinstance(source, str) else source
    provider = "gemini" if snapshot_source == "agy" else snapshot_source
    return context_length_for_model(provider, model)


def context_window_from_raw_message(raw_json: object) -> int | None:
    """Extract provider-reported context window metadata from a raw transcript object."""
    if not isinstance(raw_json, dict):
        return None
    for candidate in _context_window_candidates(raw_json):
        coerced = _coerce_positive_int(candidate)
        if coerced is not None:
            return coerced
    return None


def _context_window_candidates(data: dict[Any, Any]) -> list[object]:
    candidates: list[object] = []
    for key in (
        "context_window",
        "contextWindow",
        "context_window_size",
        "contextWindowSize",
        "model_context_window",
        "modelContextWindow",
        "total_context_tokens",
        "totalContextTokens",
    ):
        candidates.append(data.get(key))

    for nested_key in ("payload", "info", "usage", "tokenUsage", "token_usage", "params", "update"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            candidates.extend(_context_window_candidates(nested))
    return candidates


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return None
    return coerced if coerced > 0 else None
