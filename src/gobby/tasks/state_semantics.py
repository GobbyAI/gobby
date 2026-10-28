"""Shared task state semantics for task projections."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, cast, get_args

AWAITING_HUMAN_REVIEW_LABEL = "awaiting-human-review"
TaskState = Literal[
    "open", "in_progress", "blocked", "needs_human", "escalated", "deferred", "closed"
]
TaskHoldState = Literal["needs_human", "deferred"]
TaskCloseReason = Literal["done", "moot", "superseded", "wont_do"]
TASK_STATES = frozenset(get_args(TaskState))
TASK_HOLD_STATES = frozenset(get_args(TaskHoldState))
TASK_CLOSE_REASONS = frozenset(get_args(TaskCloseReason))
# No-work closes are Orchestrator overrides: they carry no linked work and record no step records.
NO_WORK_CLOSE_REASONS = frozenset({"moot", "superseded", "wont_do"})


def _read_field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _has_timestamp(value: Any) -> bool:
    return isinstance(value, datetime) or (isinstance(value, str) and bool(value))


def validate_task_state(value: object) -> TaskState:
    """Reject retired and unknown state values at query boundaries."""
    if not isinstance(value, str) or value not in TASK_STATES:
        raise ValueError(
            f"Unknown task state {value!r}; expected one of: {', '.join(sorted(TASK_STATES))}"
        )
    return cast(TaskState, value)


def validate_task_close_reason(value: object) -> TaskCloseReason:
    """Require a canonical reason; narrative evidence belongs in the close summary."""
    if not isinstance(value, str) or value not in TASK_CLOSE_REASONS:
        raise ValueError(
            f"Unknown task close reason {value!r}; "
            f"expected one of: {', '.join(sorted(TASK_CLOSE_REASONS))}"
        )
    return cast(TaskCloseReason, value)


def get_task_hold_state(task: Any) -> TaskHoldState | None:
    """Read an explicit hold, then the existing human-review label."""
    state = _read_field(task, "state")
    hold = _read_field(state, "hold_state") or _read_field(task, "hold_state")
    if isinstance(hold, str) and hold in TASK_HOLD_STATES:
        return cast(TaskHoldState, hold)
    return "needs_human" if is_awaiting_human_review(task) else None


def validate_task_hold_state(value: object, follow_up_at: object | None) -> TaskHoldState | None:
    """Validate hold data without choosing routing or follow-up behavior."""
    if value is None:
        return None
    if not isinstance(value, str) or value not in TASK_HOLD_STATES:
        raise ValueError(f"Unknown task hold state: {value!r}")
    if value == "deferred" and follow_up_at is None:
        raise ValueError("Deferred tasks require a follow-up date")
    return cast(TaskHoldState, value)


def is_task_blocked(task: Any) -> bool:
    """Read unresolved dependencies and the existing merge hold."""
    state = _read_field(task, "state")
    return bool(
        _read_field(task, "active_blocked_by")
        or _read_field(task, "blocked_by_merge") is True
        or _read_field(state, "is_blocked") is True
        or _read_field(task, "is_blocked") is True
    )


def projected_task_state(task: Any, *, is_blocked: bool | None = None) -> TaskState:
    """Return the canonical workflow-facing task state."""
    if is_task_closed(task):
        return "closed"
    if _task_is_escalated(task):
        return "escalated"
    if hold := get_task_hold_state(task):
        return hold
    if is_task_blocked(task) if is_blocked is None else is_blocked:
        return "blocked"
    if get_claimed_session_id(task):
        return "in_progress"
    return "open"


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
    """An open task, escalated ones included, belongs to its claiming session."""
    return not is_task_closed(task) and get_claimed_session_id(task) == session_id


def serialize_task_state(task: Any, *, is_blocked: bool | None = None) -> dict[str, Any]:
    """Build the canonical task-state projection for external callers."""
    owner_session_id = get_claimed_session_id(task)
    is_escalated = _task_is_escalated(task)
    name = projected_task_state(task, is_blocked=is_blocked)
    state = _read_field(task, "state")

    return {
        "name": name,
        "hold_state": get_task_hold_state(task),
        "follow_up_at": _read_field(state, "follow_up_at") or _read_field(task, "follow_up_at"),
        "owner_session_id": owner_session_id,
        "is_claimed": bool(owner_session_id),
        "is_closed": is_task_closed(task),
        "is_escalated": is_escalated,
        "is_blocked": name == "blocked",
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
