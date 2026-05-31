"""Helpers for session-level context usage snapshots."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from gobby.llm.context_windows import resolve_context_window
from gobby.storage.context_usage_snapshot import ContextUsageSnapshot, ContextUsageSource

if TYPE_CHECKING:
    from gobby.sessions.transcripts.base import TokenUsage
    from gobby.storage.hub.protocol import HubDatabase

_SOURCES: frozenset[str] = frozenset(
    {"claude", "codex", "gemini", "qwen", "droid", "agy", "grok", "web_chat"}
)
logger = logging.getLogger(__name__)


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
    """Resolve context-window metadata for a provider/model pair."""
    snapshot_source = normalize_context_usage_source(source) if isinstance(source, str) else source
    provider = "gemini" if snapshot_source == "agy" else snapshot_source
    return resolve_context_window(model, provider=provider)


def effective_context_window_for_session(
    session: Any,
    *,
    variables: dict[str, Any] | None = None,
    db: HubDatabase | None = None,
    catalog: Any | None = None,
) -> int | None:
    """Return the best context window for session hydration payloads."""
    live_window = _context_window_from_variables(variables or {})
    if live_window is not None:
        return live_window

    event_window = _latest_token_event_context_window(db, getattr(session, "id", None))
    if event_window is not None:
        return event_window

    reported_session_window = _reported_session_context_window(session, db)
    if reported_session_window is not None:
        return reported_session_window

    model = _effective_session_model(session, variables or {})
    provider = _provider_for_session(session)
    resolved = resolve_context_window(model, provider=provider, catalog=catalog)
    if resolved is not None:
        return resolved

    return _coerce_positive_int(getattr(session, "context_window", None))


def _context_window_from_variables(variables: dict[str, Any]) -> int | None:
    for name in ("context_window", "model_context_window", "modelContextWindow"):
        window = _coerce_positive_int(variables.get(name))
        if window is not None:
            return window
    return None


def _effective_session_model(session: Any, variables: dict[str, Any]) -> str | None:
    for name in ("model", "model_id", "modelId"):
        value = variables.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    model = getattr(session, "model", None)
    return model.strip() if isinstance(model, str) and model.strip() else None


def _provider_for_session(session: Any) -> str | None:
    source = getattr(session, "source", None)
    snapshot_source = normalize_context_usage_source(source if isinstance(source, str) else None)
    if snapshot_source == "agy":
        return "gemini"
    return snapshot_source


def _latest_token_event_context_window(
    db: HubDatabase | None,
    session_id: Any,
) -> int | None:
    if db is None or not isinstance(session_id, str) or not session_id:
        return None
    try:
        from gobby.storage.token_events import TokenEventStore

        # list_session_events returns newest-first rows; first context_window wins.
        events = TokenEventStore(db).list_session_events(session_id, limit=20)
    except ImportError:
        logger.debug("Token event store is unavailable", exc_info=True)
        return None
    except Exception:
        logger.warning(
            "Failed to load token event context window",
            extra={"session_id": session_id},
            exc_info=True,
        )
        return None
    if not isinstance(events, list):
        return None
    for event in events:
        if isinstance(event, Mapping):
            window = _coerce_positive_int(event.get("context_window"))
            if window is not None:
                return window
    return None


def _reported_session_context_window(session: Any, db: HubDatabase | None) -> int | None:
    session_window = _coerce_positive_int(getattr(session, "context_window", None))
    confidence = getattr(session, "context_usage_confidence", None)
    if session_window is not None and confidence == "reported":
        return session_window

    session_id = getattr(session, "id", None)
    if db is None or not isinstance(session_id, str) or not session_id:
        return None
    try:
        row = db.fetchone(
            "SELECT context_window, context_usage_confidence FROM sessions WHERE id = %s",
            (session_id,),
        )
    except Exception:
        logger.warning(
            "Failed to load reported session context window",
            extra={"session_id": session_id},
            exc_info=True,
        )
        return None
    if not isinstance(row, Mapping) or row.get("context_usage_confidence") != "reported":
        return None
    return _coerce_positive_int(row.get("context_window"))


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
        if isinstance(value, int):
            coerced = value
        elif isinstance(value, float):
            coerced = int(value)
        elif isinstance(value, str | bytes | bytearray):
            coerced = int(value)
        else:
            return None
    except (TypeError, ValueError):
        return None
    return coerced if coerced > 0 else None
