"""Helpers for deciding whether a handoff belongs to a new session."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.sessions.tmux_context import parse_terminal_context_value


def terminal_context_matches_session(
    session: Any,
    child_context: Mapping[str, Any] | str | None,
) -> bool:
    """Return whether ``child_context`` proves continuity with ``session``."""
    child = parse_terminal_context_value(child_context)
    if not child:
        return False

    session_id = getattr(session, "id", None)
    gobby_session_id = child.get("gobby_session_id")
    if isinstance(session_id, str) and gobby_session_id == session_id:
        return True

    return terminal_contexts_match(getattr(session, "terminal_context", None), child)


def sessions_have_continuous_terminal_context(first: Any, second: Any) -> bool:
    """Return whether two stored sessions look like the same terminal continuation."""
    first_id = getattr(first, "id", None)
    second_id = getattr(second, "id", None)
    first_context = parse_terminal_context_value(getattr(first, "terminal_context", None))
    second_context = parse_terminal_context_value(getattr(second, "terminal_context", None))
    if not first_context or not second_context:
        return False

    if isinstance(first_id, str) and second_context.get("gobby_session_id") == first_id:
        return True
    if isinstance(second_id, str) and first_context.get("gobby_session_id") == second_id:
        return True

    return terminal_contexts_match(first_context, second_context)


def terminal_contexts_match(
    first: Mapping[str, Any] | str | None,
    second: Mapping[str, Any] | str | None,
) -> bool:
    """Return whether two terminal contexts identify the same terminal."""
    first_context = parse_terminal_context_value(first)
    second_context = parse_terminal_context_value(second)
    if not first_context or not second_context:
        return False

    for field_name in ("tmux_pane", "tmux_session"):
        if _non_empty_equal(first_context, second_context, field_name):
            return _tmux_scope_compatible(first_context, second_context)

    for field_name in ("tty", "parent_pid"):
        if _non_empty_equal(first_context, second_context, field_name):
            return True

    return False


def _non_empty_equal(
    first_context: Mapping[str, Any],
    second_context: Mapping[str, Any],
    field_name: str,
) -> bool:
    first_value = first_context.get(field_name)
    second_value = second_context.get(field_name)
    return bool(first_value) and first_value == second_value


def _tmux_scope_compatible(
    first_context: Mapping[str, Any],
    second_context: Mapping[str, Any],
) -> bool:
    first_scope = _tmux_scope(first_context)
    second_scope = _tmux_scope(second_context)
    return first_scope is None or second_scope is None or first_scope == second_scope


def _tmux_scope(context: Mapping[str, Any]) -> tuple[str, Any] | None:
    socket_path = context.get("tmux_socket_path")
    if socket_path:
        return ("path", socket_path)
    socket_name = context.get("tmux_socket_name")
    if socket_name:
        return ("name", socket_name)
    return None
