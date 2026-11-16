"""Helpers for deciding whether a handoff belongs to a new session."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.terminal_context import parse_terminal_context_value


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

    terminal_id = first_context.get("gobby_terminal_id")
    if (
        isinstance(terminal_id, str)
        and terminal_id
        and terminal_id == second_context.get("gobby_terminal_id")
    ):
        return True

    for field_name in ("tty", "parent_pid"):
        if _non_empty_equal(first_context, second_context, field_name):
            return True

    return False


def terminal_process_contexts_match(
    first: Mapping[str, Any] | str | None,
    second: Mapping[str, Any] | str | None,
) -> bool:
    """Return whether two contexts identify the same live terminal process."""
    first_context = parse_terminal_context_value(first)
    second_context = parse_terminal_context_value(second)
    if not first_context or not second_context:
        return False
    if not terminal_contexts_match(first_context, second_context):
        return False

    return _positive_equal(first_context, second_context, "parent_pid") and _positive_equal(
        first_context,
        second_context,
        "parent_create_time",
    )


def _non_empty_equal(
    first_context: Mapping[str, Any],
    second_context: Mapping[str, Any],
    field_name: str,
) -> bool:
    first_value = first_context.get(field_name)
    second_value = second_context.get(field_name)
    return bool(first_value) and first_value == second_value


def _positive_equal(
    first_context: Mapping[str, Any],
    second_context: Mapping[str, Any],
    field_name: str,
) -> bool:
    first_value = first_context.get(field_name)
    second_value = second_context.get(field_name)
    if not isinstance(first_value, (int, float, str)):
        return False
    if not isinstance(second_value, (int, float, str)):
        return False
    try:
        return float(first_value) > 0 and float(first_value) == float(second_value)
    except (TypeError, ValueError):
        return False
