"""Pure helpers for matching session discovery candidates."""

from __future__ import annotations

import json
from typing import Any

from gobby.storage.session_models import Session

_TERMINAL_CONTEXT_FILTER_FIELDS = (
    "tmux_pane",
    "tmux_socket_path",
    "tmux_session",
    "tty",
    "term_session_id",
)


def parse_terminal_context_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_context_parent_pid(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _non_empty_text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _terminal_context_match_score(
    requested_context: dict[str, Any],
    stored_context: dict[str, Any],
) -> int | None:
    score = 0
    for field_name in _TERMINAL_CONTEXT_FILTER_FIELDS:
        requested_value = _non_empty_text(requested_context.get(field_name))
        stored_value = _non_empty_text(stored_context.get(field_name))
        if not requested_value or not stored_value:
            continue
        if requested_value != stored_value:
            return None
        score += 1
    return score


def terminal_session_match_score(
    session: Session,
    requested_context: dict[str, Any],
    parent_pid: int | None,
) -> int | None:
    stored_context = session.terminal_context or {}
    stored_parent_pid = normalize_context_parent_pid(stored_context.get("parent_pid"))
    pid_match = parent_pid is not None and stored_parent_pid == parent_pid
    match_score = _terminal_context_match_score(requested_context, stored_context)
    if match_score is None or (not pid_match and match_score == 0):
        return None
    return match_score + 100 if pid_match else match_score


def unique_best_match(matches: list[tuple[int, Session]]) -> Session | None:
    best_score = max(score for score, _session in matches)
    best_matches = [session for score, session in matches if score == best_score]
    return best_matches[0] if len(best_matches) == 1 else None


def handoff_candidate_matches(session: Session, requested_context: dict[str, Any]) -> bool:
    """Return whether a handoff candidate matches the child's terminal identity."""
    if requested_context.get("gobby_session_id") == session.id:
        return True
    score = _terminal_context_match_score(requested_context, session.terminal_context or {})
    return score is not None and score > 0
