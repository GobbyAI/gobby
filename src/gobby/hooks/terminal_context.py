"""Hook terminal-context helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def is_gobby_acp_child(terminal_context: object) -> bool:
    """Return whether terminal metadata marks a daemon-owned ACP child process."""
    return isinstance(terminal_context, Mapping) and terminal_context.get("gobby_acp_child") == "1"


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
        stripped = value.strip()
        return stripped if stripped else None
    return None
