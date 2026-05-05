"""Shared task state semantics for stage-native task projections."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

ACTIVE_STAGE_STATES: tuple[str, ...] = (
    "ready",
    "in_progress",
    "needs_review",
    "review_approved",
)


def _read_field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _stage_position(stage: Any) -> tuple[int, str]:
    raw_position = _read_field(stage, "position")
    try:
        position = int(raw_position)
    except (TypeError, ValueError):
        position = 0
    return position, str(_read_field(stage, "name", "stage_name") or "")


def current_stage(task: Any) -> Any | None:
    """Return the first incomplete stage row available on a task-like object."""
    if task is None:
        return None

    state_payload = _read_field(task, "state")
    if isinstance(state_payload, dict):
        direct_from_state = state_payload.get("current_stage")
        if direct_from_state is not None:
            return direct_from_state

    direct = _read_field(task, "current_stage")
    if direct is not None:
        return direct

    stages = _read_field(task, "stages")
    if not isinstance(stages, Sequence) or isinstance(stages, str | bytes | bytearray):
        return None

    pending = [stage for stage in stages if _read_field(stage, "state") != "done"]
    if not pending:
        return None
    return min(pending, key=_stage_position)


def current_stage_state(task: Any) -> str | None:
    stage = current_stage(task)
    raw_state = _read_field(stage, "state")
    return raw_state if isinstance(raw_state, str) else None


def projected_task_state(task: Any) -> str:
    """Return the canonical workflow-facing task state."""
    if is_task_closed(task):
        return "closed"
    if _task_is_escalated(task):
        return "escalated"
    return current_stage_state(task) or "ready"


def is_task_closed(task: Any) -> bool:
    """Return whether close metadata marks the task as closed."""
    if task is None:
        return False
    state_payload = _read_field(task, "state")
    if isinstance(state_payload, dict):
        if state_payload.get("is_closed") is True:
            return True
        state_closed_at = state_payload.get("closed_at")
        if isinstance(state_closed_at, str) and bool(state_closed_at):
            return True
    closed_at = _read_field(task, "closed_at")
    return isinstance(closed_at, str) and bool(closed_at)


def _task_is_escalated(task: Any) -> bool:
    if task is None or is_task_closed(task):
        return False
    state_payload = _read_field(task, "state")
    if isinstance(state_payload, dict):
        if state_payload.get("is_escalated") is True:
            return True
        state_escalated_at = state_payload.get("escalated_at")
        if isinstance(state_escalated_at, str) and bool(state_escalated_at):
            return True
    raw_flag = _read_field(task, "is_escalated")
    escalated_at = _read_field(task, "escalated_at")
    return raw_flag is True or (isinstance(escalated_at, str) and bool(escalated_at))


is_task_escalated = _task_is_escalated


def is_task_merge_ready(task: Any) -> bool:
    """Return whether the current stage has completed review but is not done."""
    if task is None or is_task_closed(task) or _task_is_escalated(task):
        return False
    stage = current_stage(task)
    return bool(_read_field(stage, "state") == "review_approved")


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
    assignee = _read_field(task, "assignee")
    if isinstance(assignee, str) and assignee:
        return assignee
    return None


def is_task_actionable(task: Any) -> bool:
    """Return whether a task can still participate in stage work."""
    if task is None or is_task_closed(task) or _task_is_escalated(task):
        return False
    stage_state = current_stage_state(task)
    return stage_state in ACTIVE_STAGE_STATES


def is_task_actively_claimed(task: Any, session_id: str | None = None) -> bool:
    """Return whether a task has an owner and remains in active stage work."""
    if task is None or not is_task_actionable(task):
        return False

    owner = get_claimed_session_id(task)
    if session_id is None:
        return bool(owner)
    return owner == session_id


def _current_stage_payload(task: Any) -> dict[str, str] | None:
    stage = current_stage(task)
    if stage is None:
        return None
    name = _read_field(stage, "name", "stage_name")
    state = _read_field(stage, "state")
    if not isinstance(name, str) or not isinstance(state, str):
        return None
    payload = {"name": name, "state": state}
    for field_name in ("display_name", "display_label", "category"):
        value = _read_field(stage, field_name)
        if isinstance(value, str):
            payload[field_name] = value
    return payload


def serialize_task_state(task: Any, *, is_blocked: bool | None = None) -> dict[str, Any]:
    """Build the canonical task-state projection for external callers."""
    owner_session_id = get_claimed_session_id(task)
    is_escalated = _task_is_escalated(task)
    if is_blocked is None:
        active_blocked_by = _read_field(task, "active_blocked_by")
        is_blocked = bool(active_blocked_by) or is_escalated

    return {
        "owner_session_id": owner_session_id,
        "current_stage": _current_stage_payload(task),
        "is_claimed": bool(owner_session_id),
        "is_closed": is_task_closed(task),
        "is_escalated": is_escalated,
        "is_blocked": bool(is_blocked),
        "is_merge_ready": is_task_merge_ready(task),
        "closed_at": _read_field(task, "closed_at"),
        "closed_reason": _read_field(task, "closed_reason"),
        "closed_in_session_id": _read_field(task, "closed_in_session_id"),
        "closed_commit_sha": _read_field(task, "closed_commit_sha"),
        "escalated_at": _read_field(task, "escalated_at"),
        "escalation_reason": _read_field(task, "escalation_reason"),
        "allow_automation": bool(_read_field(task, "allow_automation")),
        "unattended": bool(_read_field(task, "unattended")),
        "isolation": _read_field(task, "isolation") or "worktree",
        "assigned_agent": _read_field(task, "assigned_agent"),
        "additional_skills": _read_field(task, "additional_skills"),
    }
