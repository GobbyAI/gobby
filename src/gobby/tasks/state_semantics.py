"""Shared task state semantics for task projections."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

AWAITING_HUMAN_REVIEW_LABEL = "awaiting-human-review"


def _read_field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _has_timestamp(value: Any) -> bool:
    return isinstance(value, datetime) or (isinstance(value, str) and bool(value))





def projected_task_state(task: Any) -> str:
    """Return the canonical workflow-facing task state."""
    if is_task_closed(task):
        return "closed"
    if _task_is_escalated(task):
        return "escalated"
    return "ready"


def is_awaiting_human_review(task: Any) -> bool:
    """Return whether a task is parked for explicit human review."""
    labels = _read_field(task, "labels")
    return (
        isinstance(labels, Sequence)
        and not isinstance(labels, str)
        and (AWAITING_HUMAN_REVIEW_LABEL in labels)
    )


def is_task_closed(task: Any) -> bool:
    """Return whether close metadata marks the task as closed."""
    if task is None:
        return False
    state_payload = _read_field(task, "state")
    if isinstance(state_payload, dict):
        if state_payload.get("is_closed") is True:
            return True
        state_closed_at = state_payload.get("closed_at")
        if _has_timestamp(state_closed_at):
            return True
    closed_at = _read_field(task, "closed_at")
    return _has_timestamp(closed_at)


def _task_is_escalated(task: Any) -> bool:
    if task is None:
        return False
    state_payload = _read_field(task, "state")
    if isinstance(state_payload, dict):
        if state_payload.get("is_escalated") is True:
            return True
        state_escalated_at = state_payload.get("escalated_at")
        if _has_timestamp(state_escalated_at):
            return True
    raw_flag = _read_field(task, "is_escalated")
    escalated_at = _read_field(task, "escalated_at")
    return raw_flag is True or _has_timestamp(escalated_at)


is_task_escalated = _task_is_escalated



def get_claimed_session_id(task: Any) -> str | None:
    """Return the best available owning session ID for a task-like object."""
    if task is None:
        return None
    state_payload = _read_field(task, "state")
    if isinstance(state_payload, dict):
        owner_session_id = state_payload.get("owner_session_id")
        if isinstance(owner_session_id, str) and owner_session_id:
            return owner_session_id
    claimed_by_session_id = _read_field(task, "claimed_by_session_id")
    if isinstance(claimed_by_session_id, str) and claimed_by_session_id:
        return claimed_by_session_id
    return None


def is_task_actionable(task: Any) -> bool:
    """Return whether a task exists, is open and is not escalated."""
    return task is not None and not is_task_closed(task) and not _task_is_escalated(task)


def is_task_reviewable(task: Any) -> bool:
    """Return whether a closed task can host read-only review work.

    Escalation remains a refusal state even if inconsistent legacy data also
    carries a closure timestamp.
    """
    return task is not None and is_task_closed(task) and not _task_is_escalated(task)


def is_task_actively_claimed(task: Any, session_id: str | None = None) -> bool:
    """Return whether a task has an owner and remains actionable."""
    if task is None or not is_task_actionable(task):
        return False

    owner = get_claimed_session_id(task)
    return session_id is not None and owner == session_id


def is_task_claimed_by_session(task: Any, session_id: str) -> bool:
    """Ownership includes an escalated task claimed by its close reviewer."""
    return not is_task_closed(task) and get_claimed_session_id(task) == session_id



def serialize_task_state(task: Any, *, is_blocked: bool | None = None) -> dict[str, Any]:
    """Build the canonical task-state projection for external callers."""
    owner_session_id = get_claimed_session_id(task)
    is_escalated = _task_is_escalated(task)
    if is_blocked is None:
        active_blocked_by = _read_field(task, "active_blocked_by")
        is_blocked = bool(active_blocked_by) or is_escalated or is_awaiting_human_review(task)

    return {
        "owner_session_id": owner_session_id,
        "is_claimed": bool(owner_session_id),
        "is_closed": is_task_closed(task),
        "is_escalated": is_escalated,
        "is_blocked": bool(is_blocked),
        "closed_at": _read_field(task, "closed_at"),
        "closed_reason": _read_field(task, "closed_reason"),
        "closed_in_session_id": _read_field(task, "closed_in_session_id"),
        "closed_commit_sha": _read_field(task, "closed_commit_sha"),
        "escalated_at": _read_field(task, "escalated_at"),
        "escalation_reason": _read_field(task, "escalation_reason"),
        "assigned_agent": _read_field(task, "assigned_agent"),
        "implementation_domain": _read_field(task, "implementation_domain"),
        "additional_skills": _read_field(task, "additional_skills"),
    }
