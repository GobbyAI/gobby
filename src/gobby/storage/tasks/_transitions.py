"""Ownership-aware task transition helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from gobby.plans.bootstrap_ledger import bootstrap_ledger_path_for_task, verify_bootstrap_ledger
from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._crud import _session_exists, get_task, update_task
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import (
    UNSET,
    MaybeUnset,
    Task,
    TaskAlreadyClaimedError,
    TaskClosedError,
)
from gobby.storage.tasks._stage_states import (
    NoCurrentStageError,
    StageStatesManager,
    _close_task_in_txn,
)
from gobby.tasks.state_semantics import is_task_closed


def _stage_states(db: DatabaseProtocol) -> StageStatesManager:
    return StageStatesManager(db, TaskLifecycleEventManager(db))


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
    """Claim a task for a session."""
    task = get_task(db, task_id)
    current_owner = get_effective_claim_owner(task, db)

    if is_task_closed(task):
        raise TaskClosedError(f"Cannot claim task {task_id}: task is closed")
    if current_owner and current_owner != session_id and not force:
        raise TaskAlreadyClaimedError(task_id, current_owner)

    update_task(
        db,
        task_id,
        assignee=session_id,
        claimed_by_session_id=session_id,
    )
    return get_task(db, task_id)


def release_task_claim(
    db: DatabaseProtocol,
    task_id: str,
    *,
    description: MaybeUnset[str | None] = UNSET,
    validation_fail_count: MaybeUnset[int | None] = UNSET,
    dispatch_failure_count: MaybeUnset[int | None] = UNSET,
    escalated_at: MaybeUnset[str | None] = UNSET,
    escalation_reason: MaybeUnset[str | None] = UNSET,
    validation_override_reason: MaybeUnset[str | None] = UNSET,
    labels: MaybeUnset[list[str] | None] = UNSET,
) -> Task:
    """Clear ownership while optionally changing recovery metadata."""
    update_task(
        db,
        task_id,
        description=description,
        assignee=None,
        claimed_by_session_id=None,
        validation_fail_count=validation_fail_count,
        dispatch_failure_count=dispatch_failure_count,
        escalated_at=escalated_at,
        escalation_reason=escalation_reason,
        validation_override_reason=validation_override_reason,
        labels=labels,
    )
    return get_task(db, task_id)


def reopen_task(
    db: DatabaseProtocol,
    task_id: str,
    *,
    reason: str | None = None,
) -> Task:
    """Reopen a task and clear ownership/closure metadata."""
    task = get_task(db, task_id)
    if not is_task_closed(task) and not task.is_escalated:
        raise ValueError(f"Task {task_id} is not closed or escalated")

    description = task.description
    if reason:
        reopen_note = f"\n\n[Reopened: {reason}]"
        description = (description or "") + reopen_note

    update_task(
        db,
        task_id,
        description=description if reason else UNSET,
        assignee=None,
        claimed_by_session_id=None,
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
    if task.is_escalated or is_task_closed(task):
        raise ValueError(f"Cannot escalate task {task_id}: task is closed or escalated.")

    return release_task_claim(
        db,
        task_id,
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
    reset_validation: bool = False,
) -> Task:
    """Clear escalation state and keep the current stage unchanged."""
    task = get_task(db, task_id)
    if not task.is_escalated:
        raise ValueError(f"Task {task_id} is not escalated")

    description = (
        f"{task.description}\n\nDe-escalated: {reason}"
        if task.description
        else f"De-escalated: {reason}"
    )

    update_task(
        db,
        task_id,
        description=description,
        escalated_at=None,
        escalation_reason=None,
        validation_fail_count=0 if reset_validation else UNSET,
    )
    return get_task(db, task_id)


def submit_for_review(
    db: DatabaseProtocol,
    task_id: str,
    stage_name: str | None = None,
    *,
    review_notes: str | None = None,
    by_session_id: str | None = None,
) -> Task:
    """Submit a stage for review and release ownership."""
    task = get_task(db, task_id)
    stages = _stage_states(db)
    if stage_name is None:
        current = stages.current_stage(task_id)
        if current is None:
            raise NoCurrentStageError(task_id)
        stage_name = current.stage_name
    stages.submit_for_review(
        task_id,
        stage_name,
        by_session_id=by_session_id,
        notes=review_notes,
    )
    description: MaybeUnset[str | None] = UNSET
    if review_notes:
        description = (task.description or "") + f"\n\n[Review Notes]\n{review_notes}"
    labels = [
        label for label in (task.labels or []) if label != "planning-current-verdict:rejected"
    ]

    update_task(
        db,
        task_id,
        description=description,
        labels=labels,
        assignee=None,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)


def approve_review(
    db: DatabaseProtocol,
    task_id: str,
    stage_name: str | None = None,
    *,
    approval_notes: str | None = None,
    by_session_id: str | None = None,
) -> Task:
    """Approve review on a stage and release ownership."""
    task = get_task(db, task_id)
    stages = _stage_states(db)
    if stage_name is None:
        current = stages.current_stage(task_id)
        if current is None:
            raise NoCurrentStageError(task_id)
        stage_name = current.stage_name
    stages.approve_review(
        task_id,
        stage_name,
        by_session_id=by_session_id,
        notes=approval_notes,
    )
    description: MaybeUnset[str | None] = UNSET
    if approval_notes:
        description = (task.description or "") + f"\n\n[Approval Notes]\n{approval_notes}"

    update_task(
        db,
        task_id,
        description=description,
        assignee=None,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)


def reject_review(
    db: DatabaseProtocol,
    task_id: str,
    stage_name: str | None = None,
    *,
    rejection_notes: str | None = None,
    round_number: int | None = None,
    plan_hash: str | None = None,
    cited_subtasks: list[str] | None = None,
    by_session_id: str | None = None,
) -> Task:
    """Reject review on a stage and release ownership."""
    task = get_task(db, task_id)
    normalized_round = None
    if round_number is not None:
        # Tools/routes may pass an int-like value; normalize once before validation.
        normalized_round = int(round_number)
        if normalized_round < 1:
            raise ValueError("round must be >= 1 when provided")

    stages = _stage_states(db)
    if stage_name is None:
        current = stages.current_stage(task_id)
        if current is None:
            raise NoCurrentStageError(task_id)
        stage_name = current.stage_name
    notes = rejection_notes
    if plan_hash:
        notes = f"{notes or ''}\n\nplan_hash: {plan_hash}".strip()
    if cited_subtasks:
        notes = f"{notes or ''}\n\ncited_subtasks: {', '.join(cited_subtasks)}".strip()
    stages.reject_review(
        task_id,
        stage_name,
        reason=rejection_notes or "review_rejected",
        by_session_id=by_session_id,
        notes=notes,
    )

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

    update_task(
        db,
        task_id,
        description=description,
        assignee=None,
        claimed_by_session_id=None,
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
    with db.transaction() as conn:
        if bootstrap_ledger_path_for_task(db, task_id) is not None:
            verify_bootstrap_ledger(db, task_id)
        _close_task_in_txn(
            conn,
            task_id,
            reason=reason,
            commit_sha=closed_commit_sha,
            closed_in_session_id=closed_in_session_id,
            force=force,
            validation_override_reason=validation_override_reason,
        )
    return get_task(db, task_id)


def reconcile_task_state(
    db: DatabaseProtocol,
    task_id: str,
    *,
    title: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    priority: MaybeUnset[int | None] = UNSET,
) -> Task:
    """Apply externally-sourced metadata without reopening generic update paths.

    This helper is intentionally narrow: it exists for sync/reconciliation flows
    that need to update synced fields without exposing raw ownership mutation
    through LocalTaskManager.update_task().
    """
    update_task(
        db,
        task_id,
        title=title,
        description=description,
        priority=priority,
        assignee=None,
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)
