"""Helpers for session-level context usage snapshots."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from gobby.llm.context_windows import normalize_model_lookup_id, resolve_context_window
from gobby.storage.context_usage_snapshot import ContextUsageSnapshot, ContextUsageSource

if TYPE_CHECKING:
    from gobby.sessions.transcripts.base import TokenUsage
    from gobby.storage.hub.protocol import HubDatabase

_SOURCES: frozenset[str] = frozenset(
    {"claude", "codex", "qwen", "droid", "agy", "grok", "web_chat"}
)
logger = logging.getLogger(__name__)
_AGY_LABEL_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
_GPT_OSS_CONTEXT_WINDOW = 131_072


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
    resolved_window = _resolve_context_window_for_source_model(
        snapshot_source,
        model,
        provider_reported_context_window=context_window,
    )
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
    resolved_window = _resolve_context_window_for_source_model(
        snapshot_source,
        model,
        provider_reported_context_window=context_window,
    )
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
    *,
    overrides: dict[str, int] | None = None,
    db: HubDatabase | None = None,
) -> int | None:
    """Resolve context-window metadata for a provider/model pair."""
    snapshot_source = normalize_context_usage_source(source) if isinstance(source, str) else source
    return _resolve_context_window_for_source_model(
        snapshot_source,
        model,
        overrides=overrides,
        db=db,
    )


def _resolve_context_window_for_source_model(
    source: ContextUsageSource | None,
    model: str | None,
    *,
    provider_reported_context_window: Any | None = None,
    overrides: dict[str, int] | None = None,
    db: HubDatabase | None = None,
) -> int | None:
    """Resolve context-window metadata for a provider/model pair."""
    snapshot_source = source
    if snapshot_source == "agy":
        reported = _coerce_positive_int(provider_reported_context_window)
        resolved = resolve_context_window(
            model,
            overrides=overrides,
            provider="agy",
            provider_reported_context_window=reported,
            db=db,
        )
        return (
            resolved
            if resolved is not None
            else _context_window_for_agy_model(model, overrides, db=db)
        )
    provider = snapshot_source
    if provider_reported_context_window is None:
        return resolve_context_window(
            model,
            overrides=overrides,
            provider=provider,
            db=db,
        )
    reported = _coerce_positive_int(provider_reported_context_window)
    resolved = resolve_context_window(
        model,
        overrides=overrides,
        provider=provider,
        provider_reported_context_window=reported,
        db=db,
    )
    return resolved if resolved is not None else reported


def _context_window_for_agy_model(
    model: str | None,
    overrides: dict[str, int] | None = None,
    *,
    db: HubDatabase | None = None,
) -> int | None:
    """Resolve AGY windows by the model family currently exposed by `agy models`."""
    lookup_model = _normalize_agy_model_lookup_id(model)
    if lookup_model is None:
        return None
    if lookup_model.startswith("gemini-"):
        return resolve_context_window(
            lookup_model,
            overrides=overrides,
            provider="agy",
            db=db,
        )
    if lookup_model.startswith("claude-"):
        return resolve_context_window(
            lookup_model,
            overrides=overrides,
            provider="claude",
            db=db,
        )
    if lookup_model.startswith("gpt-oss-"):
        return _GPT_OSS_CONTEXT_WINDOW
    return None


def _normalize_agy_model_lookup_id(model: str | None) -> str | None:
    if not model:
        return None
    without_suffix = _AGY_LABEL_SUFFIX_RE.sub("", model.strip())
    normalized = re.sub(r"[^a-z0-9.]+", "-", normalize_model_lookup_id(without_suffix)).strip("-")
    if not normalized:
        return None
    return normalized


def resolve_context_window_overrides(config: object | None) -> dict[str, int] | None:
    """Return configured context-window overrides when stored as a mapping."""
    configured = getattr(config, "context_window_overrides", None)
    return cast(dict[str, int], configured) if isinstance(configured, dict) else None


def effective_context_window_for_session(
    session: Any,
    *,
    variables: dict[str, Any] | None = None,
    db: HubDatabase | None = None,
    overrides: dict[str, int] | None = None,
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
    source = getattr(session, "source", None)
    snapshot_source = normalize_context_usage_source(source if isinstance(source, str) else None)
    resolved = _resolve_context_window_for_source_model(
        snapshot_source,
        model,
        overrides=overrides,
        db=db,
    )
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
    if not isinstance(row, Mapping):
        return None
    confidence = row.get("context_usage_confidence")
    if isinstance(confidence, str) and confidence == "reported":
        return _coerce_positive_int(row.get("context_window"))
    return None


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


@dataclass(frozen=True)
class ContextWindowBackfillResult:
    """Outcome of a one-shot context-window backfill pass."""

    scanned: int
    updated: int

    @property
    def skipped(self) -> int:
        return self.scanned - self.updated


def backfill_session_context_windows(
    db: HubDatabase,
    *,
    dry_run: bool = False,
    overrides: dict[str, int] | None = None,
) -> ContextWindowBackfillResult:
    """Re-resolve under-counted session context windows from the model.

    Historical sessions persisted an under-sized ``context_window`` (e.g. a
    1M-context Opus stored at 200k), which clamped ``context_usage_ratio`` to
    100%. For each session that carries recorded usage, re-resolve its window
    from its model via the family-aware resolver and bump it upward when the
    resolved window is larger, recomputing the ratio from the stored
    ``context_used_tokens``. Windows that already meet or exceed the resolved
    value are left untouched, so genuinely-reported smaller windows (other
    providers/runtimes) are never shrunk.
    """
    rows = db.fetchall(
        "SELECT id, model, source, context_window, context_used_tokens "
        "FROM sessions "
        "WHERE model IS NOT NULL AND model <> '' "
        "AND context_used_tokens IS NOT NULL AND context_used_tokens > 0"
    )
    scanned = 0
    updated = 0
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        scanned += 1
        model = row.get("model")
        if not isinstance(model, str) or not model.strip():
            continue

        source = row.get("source")
        snapshot_source = normalize_context_usage_source(
            source if isinstance(source, str) else None
        )
        resolved = _resolve_context_window_for_source_model(
            snapshot_source,
            model,
            overrides=overrides,
            db=db,
        )
        if resolved is None:
            continue

        current_window = _coerce_positive_int(row.get("context_window"))
        if current_window is not None and current_window >= resolved:
            continue

        used = _coerce_positive_int(row.get("context_used_tokens"))
        new_ratio = ContextUsageSnapshot.calculate_ratio(used, resolved)
        updated += 1
        if dry_run:
            continue
        with db.transaction():
            db.execute(
                "UPDATE sessions SET context_window = %s, context_usage_ratio = %s, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (resolved, new_ratio, row.get("id")),
            )
    return ContextWindowBackfillResult(scanned=scanned, updated=updated)
