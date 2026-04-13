"""Shared task state semantics used during the lifecycle split migration."""

from __future__ import annotations

from typing import Any, Literal, cast

TaskLifecycleStage = Literal["in_progress", "needs_review", "review_approved"]
LegacyTaskStatus = Literal[
    "open",
    "in_progress",
    "needs_review",
    "review_approved",
    "closed",
    "escalated",
]

LIFECYCLE_STAGES: tuple[TaskLifecycleStage, ...] = (
    "in_progress",
    "needs_review",
    "review_approved",
)

ACTIVE_CLAIM_STATUSES: tuple[str, ...] = (
    "open",
    "in_progress",
    "needs_review",
    "review_approved",
    "escalated",
)

DE_ESCALATION_TARGET_STATUSES: tuple[str, ...] = (
    "open",
    "in_progress",
    "needs_review",
    "review_approved",
)


def lifecycle_stage_from_status(status: str | None) -> TaskLifecycleStage | None:
    """Map a legacy projected status back to canonical lifecycle stage."""
    if status in LIFECYCLE_STAGES:
        return cast(TaskLifecycleStage, status)
    return None


def normalize_lifecycle_stage(stage: str | None) -> TaskLifecycleStage | None:
    """Validate and normalize a lifecycle stage value."""
    if stage is None:
        return None
    normalized = stage.strip().lower().replace("-", "_")
    if normalized == "open":
        return None
    if normalized in LIFECYCLE_STAGES:
        return cast(TaskLifecycleStage, normalized)
    raise ValueError(
        f"Invalid lifecycle_stage '{stage}'. Expected one of: open, {', '.join(LIFECYCLE_STAGES)}."
    )


def project_legacy_status(
    *,
    lifecycle_stage: str | None,
    closed_at: str | None = None,
    escalated_at: str | None = None,
    legacy_status: str | None = None,
) -> LegacyTaskStatus:
    """Project canonical lifecycle fields back to the temporary legacy status surface."""
    if closed_at or legacy_status == "closed":
        return "closed"
    if escalated_at or legacy_status == "escalated":
        return "escalated"

    normalized_stage = normalize_lifecycle_stage(lifecycle_stage)
    if normalized_stage:
        return normalized_stage

    if legacy_status in ("in_progress", "needs_review", "review_approved"):
        return cast(LegacyTaskStatus, legacy_status)
    return "open"


def is_task_closed(task: Any) -> bool:
    """Return whether close metadata marks the task as closed."""
    if task is None:
        return False
    closed_at = getattr(task, "closed_at", None)
    if isinstance(closed_at, str) and bool(closed_at):
        return True
    return getattr(task, "status", None) == "closed"


def is_task_escalated(task: Any) -> bool:
    """Return whether escalation metadata marks the task as escalated."""
    if task is None or is_task_closed(task):
        return False
    escalated_at = getattr(task, "escalated_at", None)
    if isinstance(escalated_at, str) and bool(escalated_at):
        return True
    return getattr(task, "status", None) == "escalated"


def is_task_merge_ready(task: Any) -> bool:
    """Return whether the task has passed review and is ready for merge/close."""
    if task is None or is_task_closed(task) or is_task_escalated(task):
        return False
    lifecycle_stage = _coerce_task_lifecycle_stage(task)
    return lifecycle_stage == "review_approved"


def is_active_claim_status(status: str | None) -> bool:
    """Return whether a legacy status should still count as active claimed work."""
    return bool(status) and status in ACTIVE_CLAIM_STATUSES


def get_claimed_session_id(task: Any) -> str | None:
    """Return the best available owning session ID during the ownership migration."""
    if task is None:
        return None
    claimed_by_session_id = getattr(task, "claimed_by_session_id", None)
    if isinstance(claimed_by_session_id, str) and claimed_by_session_id:
        return claimed_by_session_id
    assignee = getattr(task, "assignee", None)
    return assignee if isinstance(assignee, str) and assignee else None


def is_task_actively_claimed(task: Any, session_id: str | None = None) -> bool:
    """Return whether a task still represents active claimed work.

    If ``session_id`` is provided, the task must also still be assigned to that
    session. This keeps reconciliation and recovery aligned while ownership is
    migrates from legacy ``assignee`` to canonical ``claimed_by_session_id``.
    """

    if task is None or not is_active_claim_status(getattr(task, "status", None)):
        return False

    assignee = get_claimed_session_id(task)
    if session_id is None:
        return bool(assignee)
    return assignee == session_id


def normalize_de_escalation_target_status(
    target_status: str | None,
    *,
    default: str = "open",
) -> str:
    """Validate and normalize the target status for de-escalation."""
    normalized = (target_status or default).strip().lower().replace("-", "_")
    if normalized not in DE_ESCALATION_TARGET_STATUSES:
        allowed = ", ".join(DE_ESCALATION_TARGET_STATUSES)
        raise ValueError(f"Invalid target_status '{target_status}'. Expected one of: {allowed}.")
    return normalized


def _coerce_task_lifecycle_stage(task: Any) -> TaskLifecycleStage | None:
    """Return the best-effort lifecycle stage for a task during migration."""
    if task is None:
        return None

    raw_stage = getattr(task, "lifecycle_stage", None)
    try:
        normalized_stage = normalize_lifecycle_stage(raw_stage)
    except (AttributeError, TypeError, ValueError):
        normalized_stage = None

    if normalized_stage is not None:
        return normalized_stage

    legacy_status = getattr(task, "status", None)
    if isinstance(legacy_status, str):
        return lifecycle_stage_from_status(legacy_status)
    return None


def serialize_task_state(task: Any, *, is_blocked: bool | None = None) -> dict[str, Any]:
    """Build the canonical task-state projection for external callers."""
    owner_session_id = get_claimed_session_id(task)
    lifecycle_stage = _coerce_task_lifecycle_stage(task)
    if is_blocked is None:
        active_blocked_by = getattr(task, "active_blocked_by", None)
        is_blocked = bool(active_blocked_by) or is_task_escalated(task)

    return {
        "owner_session_id": owner_session_id,
        "lifecycle_stage": lifecycle_stage,
        "is_claimed": bool(owner_session_id),
        "is_closed": is_task_closed(task),
        "is_escalated": is_task_escalated(task),
        "is_blocked": bool(is_blocked),
        "is_merge_ready": is_task_merge_ready(task),
        "closed_at": getattr(task, "closed_at", None),
        "closed_reason": getattr(task, "closed_reason", None),
        "closed_in_session_id": getattr(task, "closed_in_session_id", None),
        "closed_commit_sha": getattr(task, "closed_commit_sha", None),
        "escalated_at": getattr(task, "escalated_at", None),
        "escalation_reason": getattr(task, "escalation_reason", None),
    }
