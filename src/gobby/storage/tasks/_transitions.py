"""Ownership-aware task transition helpers.

This module centralizes task lifecycle writes that previously lived across MCP
wrappers and ad hoc storage updates. Phase 1 keeps the legacy `status` field as
the outward lifecycle surface, but makes session ownership explicit via
`claimed_by_session_id`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._crud import _session_exists, get_task, update_task
from gobby.storage.tasks._models import UNSET, MaybeUnset, Task
from gobby.tasks.state_semantics import is_task_closed, normalize_de_escalation_target_status

logger = logging.getLogger(__name__)


def project_claim_status(current_status: str) -> str:
    """Project the legacy status used when a session claims a task."""
    return "in_progress" if current_status == "open" else current_status


def get_effective_claim_owner(task: Task, db: DatabaseProtocol) -> str | None:
    """Return the canonical owning session for a task during the migration."""
    if task.claimed_by_session_id:
        return task.claimed_by_session_id
    if task.assignee and _session_exists(db, task.assignee):
        return task.assignee
    return None


def claim_task(
    db: DatabaseProtocol,
    task_id: str,
    session_id: str,
    *,
    force: bool = False,
) -> Task:
    """Claim a task for a session, preserving non-open lifecycle states."""
    task = get_task(db, task_id)
    current_owner = get_effective_claim_owner(task, db)

    if is_task_closed(task):
        raise ValueError(f"Cannot claim task {task_id}: task is closed")
    if current_owner and current_owner != session_id and not force:
        raise ValueError(f"Task {task_id} is already claimed by session '{current_owner}'")

    status = project_claim_status(task.status)
    update_task(
        db,
        task_id,
        status=status if status != task.status else UNSET,
        assignee=session_id,
        claimed_by_session_id=session_id,
    )
    return get_task(db, task_id)


def release_task_claim(
    db: DatabaseProtocol,
    task_id: str,
    *,
    status: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    validation_fail_count: MaybeUnset[int | None] = UNSET,
    dispatch_failure_count: MaybeUnset[int | None] = UNSET,
    escalated_at: MaybeUnset[str | None] = UNSET,
    escalation_reason: MaybeUnset[str | None] = UNSET,
    validation_override_reason: MaybeUnset[str | None] = UNSET,
) -> Task:
    """Clear canonical and legacy ownership while optionally changing lifecycle state."""
    update_task(
        db,
        task_id,
        status=status,
        description=description,
        assignee=None,
        claimed_by_session_id=None,
        validation_fail_count=validation_fail_count,
        dispatch_failure_count=dispatch_failure_count,
        escalated_at=escalated_at,
        escalation_reason=escalation_reason,
        validation_override_reason=validation_override_reason,
    )
    return get_task(db, task_id)


def reopen_task(
    db: DatabaseProtocol,
    task_id: str,
    *,
    reason: str | None = None,
) -> Task:
    """Reopen a task to open status and clear ownership/closure metadata."""
    task = get_task(db, task_id)
    if task.status == "open":
        raise ValueError(f"Task {task_id} is already open")

    description = task.description
    if reason:
        reopen_note = f"\n\n[Reopened: {reason}]"
        description = (description or "") + reopen_note

    update_task(
        db,
        task_id,
        status="open",
        description=description if reason else UNSET,
        assignee=None,
        claimed_by_session_id=None,
        lifecycle_stage=None,
        closed_reason=None,
        closed_at=None,
        closed_in_session_id=None,
        closed_commit_sha=None,
        escalated_at=None,
        escalation_reason=None,
        validation_fail_count=0,
        dispatch_failure_count=0,
    )
    return get_task(db, task_id)


def escalate_task(
    db: DatabaseProtocol,
    task_id: str,
    *,
    reason: str,
    validation_override_reason: str | None = None,
) -> Task:
    """Escalate a task and release canonical ownership.

    When ``validation_override_reason`` is provided, it is persisted in the
    same write as the escalation so callers don't need a second update_task
    call that could fail after the escalation has already landed.
    """
    task = get_task(db, task_id)
    if task.status == "escalated" or is_task_closed(task):
        raise ValueError(f"Cannot escalate task with status '{task.status}'.")

    return release_task_claim(
        db,
        task_id,
        status="escalated",
        escalated_at=datetime.now(UTC).isoformat(),
        escalation_reason=reason,
        validation_override_reason=(
            validation_override_reason if validation_override_reason is not None else UNSET
        ),
    )


def de_escalate_task(
    db: DatabaseProtocol,
    task_id: str,
    *,
    reason: str,
    target_status: str | None = None,
    reset_validation: bool = False,
) -> Task:
    """Return an escalated task to an explicit next status."""
    task = get_task(db, task_id)
    if task.status != "escalated":
        raise ValueError(f"Task {task_id} is not escalated (current status: {task.status})")

    normalized_target = normalize_de_escalation_target_status(target_status)
    description = (
        f"{task.description}\n\nDe-escalated: {reason}"
        if task.description
        else f"De-escalated: {reason}"
    )

    update_task(
        db,
        task_id,
        status=normalized_target,
        description=description,
        escalated_at=None,
        escalation_reason=None,
        validation_fail_count=0 if reset_validation else UNSET,
    )
    return get_task(db, task_id)


def mark_task_needs_review(
    db: DatabaseProtocol,
    task_id: str,
    *,
    review_notes: str | None = None,
) -> Task:
    """Submit a task for review and release ownership."""
    task = get_task(db, task_id)
    if is_task_closed(task) or task.status == "escalated":
        raise ValueError(
            f"Cannot mark task with status '{task.status}' as needs_review. "
            "Task must be active (not closed or escalated)."
        )

    description: MaybeUnset[str | None] = UNSET
    if review_notes:
        description = (task.description or "") + f"\n\n[Review Notes]\n{review_notes}"

    return release_task_claim(
        db,
        task_id,
        status="needs_review",
        description=description,
    )


def mark_task_review_approved(
    db: DatabaseProtocol,
    task_id: str,
    *,
    approval_notes: str | None = None,
) -> Task:
    """Approve a task after review and release ownership."""
    task = get_task(db, task_id)
    if task.status not in ("needs_review", "in_progress", "escalated"):
        raise ValueError(
            f"Cannot approve task with status '{task.status}'. "
            "Task must be in 'needs_review', 'in_progress', or 'escalated' status to approve."
        )

    description: MaybeUnset[str | None] = UNSET
    if approval_notes:
        description = (task.description or "") + f"\n\n[Approval Notes]\n{approval_notes}"

    return release_task_claim(
        db,
        task_id,
        status="review_approved",
        description=description,
    )


def mark_task_review_rejected(
    db: DatabaseProtocol,
    task_id: str,
    *,
    rejection_notes: str | None = None,
    round_number: int | None = None,
) -> Task:
    """Reject a task after review and return it to open status."""
    task = get_task(db, task_id)
    if task.status not in ("needs_review", "in_progress"):
        raise ValueError(
            f"Cannot reject review for task with status '{task.status}'. "
            "Task must be in 'needs_review' or 'in_progress' status to reject review."
        )

    normalized_round = None
    if round_number is not None:
        # Tools/routes may pass an int-like value; normalize once before validation.
        normalized_round = int(round_number)
        if normalized_round < 1:
            raise ValueError("round must be >= 1 when provided")

    description: MaybeUnset[str | None] = UNSET
    if rejection_notes:
        heading = (
            f"## Adversary Findings — Round {normalized_round}"
            if normalized_round is not None
            else "## Review Rejection"
        )
        section = f"{heading}\n\n{rejection_notes}"
        existing = task.description or ""
        # Re-running the same round must replace the prior section, not stack.
        # Mirrors the planning-round:N label dedup below — same idempotency policy.
        # Only attempt the in-place replacement for round-scoped headings; the
        # generic "## Review Rejection" heading is used for one-off rejections
        # without a round number and is allowed to stack.
        if normalized_round is not None and heading in existing:
            import re

            pattern = re.compile(
                rf"^{re.escape(heading)}.*?(?=^## Adversary Findings — Round |\Z)",
                re.DOTALL | re.MULTILINE,
            )
            description = pattern.sub(section.rstrip() + "\n\n", existing).rstrip() or section
        else:
            description = f"{existing}\n\n{section}" if existing else section

    labels = list(task.labels or [])
    if normalized_round is not None:
        labels = [label for label in labels if not label.startswith("planning-round:")]
        labels.append(f"planning-round:{normalized_round}")

    update_task(
        db,
        task_id,
        status="open",
        description=description,
        labels=labels if normalized_round is not None else UNSET,
        assignee=None,
        claimed_by_session_id=None,
        escalated_at=None,
        escalation_reason=None,
    )
    return get_task(db, task_id)


def close_task(
    db: DatabaseProtocol,
    task_id: str,
    *,
    reason: str | None = None,
    force: bool = False,
    closed_in_session_id: str | None = None,
    closed_commit_sha: str | None = None,
    validation_override_reason: str | None = None,
) -> Task:
    """Close a task and clear active ownership metadata."""
    if not force:
        open_children = db.fetchall(
            "SELECT id, title FROM tasks WHERE parent_task_id = ? AND closed_at IS NULL",
            (task_id,),
        )
        if open_children:
            child_list = ", ".join(f"{c['id']} ({c['title']})" for c in open_children[:3])
            if len(open_children) > 3:
                child_list += f" and {len(open_children) - 3} more"
            raise ValueError(
                f"Cannot close task {task_id}: has {len(open_children)} open child task(s): {child_list}"
            )

    now = datetime.now(UTC).isoformat()
    update_task(
        db,
        task_id,
        status="closed",
        assignee=None,
        claimed_by_session_id=None,
        closed_reason=reason,
        closed_at=now,
        closed_in_session_id=closed_in_session_id,
        closed_commit_sha=closed_commit_sha,
        validation_override_reason=validation_override_reason,
    )
    return get_task(db, task_id)


def reconcile_task_state(
    db: DatabaseProtocol,
    task_id: str,
    *,
    status: str,
    title: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    priority: MaybeUnset[int | None] = UNSET,
) -> Task:
    """Apply externally-sourced lifecycle state without reopening generic update paths.

    This helper is intentionally narrow: it exists for sync/reconciliation flows
    that need to project external lifecycle state into a task without exposing
    raw status/ownership mutation through LocalTaskManager.update_task().
    """
    update_task(
        db,
        task_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        assignee=None,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)
