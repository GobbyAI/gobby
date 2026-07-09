"""Pure helpers for stored terminal context values."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def parse_terminal_context_value(
    terminal_context: Mapping[str, Any] | str | None,
) -> dict[str, Any] | None:
    """Normalize stored terminal context from either JSON text or a mapping."""
    if not terminal_context:
        return None
    if isinstance(terminal_context, str):
        try:
            parsed = json.loads(terminal_context)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(terminal_context, Mapping):
        return dict(terminal_context)
    return None


def merge_terminal_context(
    current: Mapping[str, Any] | None,
    incoming: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge terminal context, preferring incoming non-null values."""
    merged: dict[str, Any] = dict(current or {})
    for key, value in (incoming or {}).items():
        if value is None:
            continue
        merged[key] = value
    return merged


def terminal_context_has_tmux_target(
    terminal_context: Mapping[str, Any] | str | None,
) -> bool:
    """Return whether terminal context identifies a tmux pane or session."""
    ctx = parse_terminal_context_value(terminal_context)
    if not ctx:
        return False
    return bool(ctx.get("tmux_pane") or ctx.get("tmux_session"))
