"""Resolve the live additionalContext character limit for a hook provider."""

from __future__ import annotations

from typing import Any

from gobby.hooks.events import SessionSource, parse_session_source
from gobby.llm.sdk_utils import (
    ADDITIONAL_CONTEXT_LIMIT,
    HANDOFF_COMPANION_RESERVE,
    INLINE_CONTEXT_HEADROOM,
)


def additional_context_limit_for(provider: SessionSource | str | None) -> int:
    """Return the ship limit for one provider, falling back to the default."""
    hooks = _active_hooks_config()
    if hooks is None:
        return ADDITIONAL_CONTEXT_LIMIT
    key = _provider_key(provider)
    overrides = getattr(hooks, "additional_context_limits", None) or {}
    if key and isinstance(overrides, dict):
        parsed = _positive_int(overrides.get(key))
        if parsed is not None:
            return parsed
    configured = _positive_int(getattr(hooks, "additional_context_limit", None))
    return configured if configured is not None else ADDITIONAL_CONTEXT_LIMIT


def inline_context_budget_for(provider: SessionSource | str | None) -> int:
    """Return the memory-inline budget, leaving room under the ship limit."""
    return max(0, additional_context_limit_for(provider) - INLINE_CONTEXT_HEADROOM)


def handoff_summary_inject_budget_for(provider: SessionSource | str | None) -> int:
    """Return the inline handoff-summary budget for one provider."""
    return max(0, additional_context_limit_for(provider) - HANDOFF_COMPANION_RESERVE)


def _provider_key(provider: SessionSource | str | None) -> str | None:
    if provider is None:
        return None
    source = parse_session_source(provider)
    if source is SessionSource.UNKNOWN and not (isinstance(provider, str) and provider.strip()):
        return None
    if source is SessionSource.UNKNOWN and isinstance(provider, str):
        return provider.strip().casefold() or None
    return str(source.value)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _active_hooks_config() -> Any | None:
    try:
        from gobby.app_context import get_app_context
    except ImportError:
        return None
    container = get_app_context()
    if container is None:
        return None
    runtime = getattr(container, "config_runtime", None)
    if runtime is None:
        return None
    try:
        return runtime.snapshot.active.hooks
    except RuntimeError:
        return None
