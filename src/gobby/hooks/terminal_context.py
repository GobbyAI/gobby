"""Hook terminal-context helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def hook_cwd(data: Mapping[str, Any], event_cwd: Any = None) -> str | None:
    """Return the first non-empty cwd supplied by hook data or event metadata."""
    return _non_empty_str(data.get("cwd")) or _non_empty_str(event_cwd)


def enrich_terminal_context_with_cwd(
    terminal_context: dict[str, Any] | None,
    cwd: Any,
) -> dict[str, Any] | None:
    """Copy terminal context and add cwd when the hook supplied one."""
    cwd_text = _non_empty_str(cwd)
    if terminal_context is None:
        return {"cwd": cwd_text} if cwd_text else None

    enriched = dict(terminal_context)
    if cwd_text and not _non_empty_str(enriched.get("cwd")):
        enriched["cwd"] = cwd_text
    return enriched


def _non_empty_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value else None
    return None
