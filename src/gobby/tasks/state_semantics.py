"""Shared task state semantics used during the status-model migration.

These helpers intentionally model the current transitional semantics rather
than the final lifecycle split. They give claim reconciliation, recovery,
and de-escalation a single definition to follow during Phase 0.
"""

from __future__ import annotations

from typing import Any

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
