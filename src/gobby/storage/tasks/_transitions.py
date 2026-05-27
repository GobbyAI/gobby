"""Ownership-aware task transition helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gobby.plans.bootstrap_ledger import bootstrap_ledger_path_for_task, verify_bootstrap_ledger
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import (
    UNSET,
    MaybeUnset,
    Task,
    TaskAlreadyClaimedError,
    TaskAlreadyEscalatedError,
    TaskClosedError,
)
from gobby.storage.tasks._ownership import _session_exists
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._stage_states import (
    StageStatesManager,
)
from gobby.storage.tasks._stage_types import NoCurrentStageError
from gobby.storage.tasks._stage_utils import _close_task_in_txn
from gobby.storage.tasks._updates import update_task
from gobby.tasks.state_semantics import is_task_closed

_WORK_ATTEMPT_ESCALATION_SUFFIXES = ("_work_failed:max", "_max_work_attempts")


def _stage_states(db: HubDatabase) -> StageStatesManager:
    return StageStatesManager(db, TaskLifecycleEventManager(db))


def _task_ref(task: Task, fallback: str) -> str:
    return f"#{task.seq_num}" if task.seq_num else fallback


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _has_active_dispatch_mutex(db: HubDatabase, task_id: str) -> bool:
    mutex = TaskDispatchMutexManager(db).get_mutex(task_id)
    if mutex is None:
        return False
    lease_until = _parse_time(mutex.lease_until)
    return lease_until is None or lease_until >= datetime.now(UTC)


def _has_active_agent_run(db: HubDatabase, task_id: str) -> bool:
    return bool(LocalAgentRunManager(db).list_active(task_ids=[task_id], limit=1))


def _active_build_automation_message(task: Task, task_id: str) -> str:
    ref = _task_ref(task, task_id)
    return (
        f"Task {ref} is controlled by active build automation. "
        f"Run gobby build stop {ref} before reopening it."
    )


def _current_stage_row(db: HubDatabase, task_id: str) -> Any | None:
    return db.fetchone(
        """
        SELECT *
          FROM task_stage_states
         WHERE task_id = %s AND state != 'done'
         ORDER BY position, stage_name
         LIMIT 1
        """,
        (task_id,),
    )


def reset_current_non_ready_stage(
    db: HubDatabase,
    task_id: str,
    *,
    reason: str,
    by_actor: str = "system",
) -> bool:
    """Reset the current non-ready stage to ready without a failure transition."""
    row = _current_stage_row(db, task_id)
    if row is None or row["state"] == "ready":
        return False

    now = datetime.now(UTC).isoformat()
    stage_name = row["stage_name"]
    from_state = row["state"]
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE task_stage_states
               SET state = 'ready',
                   entered_at = NULL,
                   entered_by_session_id = NULL,
                   completed_at = NULL,
                   completed_by_session_id = NULL,
                   completed_commit_sha = NULL,
                   artifact_refs = NULL,
                   notes = NULL,
                   updated_at = %s
             WHERE task_id = %s AND stage_name = %s
            """,
            (now, task_id, stage_name),
        )
    TaskLifecycleEventManager(db).record_lifecycle_event(
        task_id,
        f"{stage_name}:{from_state}",
        f"{stage_name}:ready",
        reason,
        by_actor=by_actor,
    )
    return True


def get_effective_claim_owner(task: Task, db: HubDatabase) -> str | None:
    """Return the canonical owning session for a task during the migration."""
    if task.claimed_by_session_id:
        return task.claimed_by_session_id
    if task.assignee and _session_exists(db, task.assignee):
        return task.assignee
    return None


def claim_task(
    db: HubDatabase,
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
    db: HubDatabase,
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
    db: HubDatabase,
    task_id: str,
    *,
    reason: str | None = None,
) -> Task:
    """Reopen a task and clear ownership/closure metadata."""
    task = get_task(db, task_id)
    if (
        task.allow_automation
        or _has_active_dispatch_mutex(db, task_id)
        or _has_active_agent_run(db, task_id)
    ):
        raise ValueError(_active_build_automation_message(task, task_id))

    current_stage = _current_stage_row(db, task_id)
    current_stage_ready = current_stage is None or current_stage["state"] == "ready"
    has_ownership_metadata = bool(task.claimed_by_session_id or task.assignee)
    if (
        not is_task_closed(task)
        and not task.is_escalated
        and current_stage_ready
        and not has_ownership_metadata
    ):
        raise ValueError(f"Task {_task_ref(task, task_id)} is already ready")

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
    reset_current_non_ready_stage(
        db,
        task_id,
        reason="reopen_task",
        by_actor="system",
    )
    return get_task(db, task_id)


def escalate_task(
    db: HubDatabase,
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
    if task.is_escalated:
        raise TaskAlreadyEscalatedError(task_id, task.escalation_reason)
    if is_task_closed(task):
        raise ValueError(f"Cannot escalate task {task_id}: task is closed.")

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
    db: HubDatabase,
    task_id: str,
    *,
    reason: str,
    reset_validation: bool = False,
    reset_stage_attempts: bool = False,
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

    release_task_claim(
        db,
        task_id,
        description=description,
        escalated_at=None,
        escalation_reason=None,
        validation_fail_count=0 if reset_validation else UNSET,
    )
    if reset_stage_attempts:
        _reset_stage_work_attempts_for_de_escalation(
            db,
            task_id,
            reason=reason,
            escalation_reason=task.escalation_reason,
        )
    return get_task(db, task_id)


def _reset_stage_work_attempts_for_de_escalation(
    db: HubDatabase,
    task_id: str,
    *,
    reason: str,
    escalation_reason: str | None,
) -> None:
    stage_name = _stage_name_from_work_attempt_escalation(db, task_id, escalation_reason)
    if stage_name is not None and _reset_stage_work_attempts(db, task_id, stage_name, reason):
        return

    current = _current_stage_row(db, task_id)
    if current is None:
        return
    _reset_stage_work_attempts(db, task_id, str(current["stage_name"]), reason)


def _stage_name_from_work_attempt_escalation(
    db: HubDatabase,
    task_id: str,
    escalation_reason: str | None,
) -> str | None:
    if not escalation_reason:
        return None

    rows = db.fetchall(
        """
        SELECT stage_name
          FROM task_stage_states
         WHERE task_id = %s
        """,
        (task_id,),
    )
    stage_names = sorted((str(row["stage_name"]) for row in rows), key=len, reverse=True)
    for stage_name in stage_names:
        if any(
            escalation_reason == f"{stage_name}{suffix}"
            for suffix in _WORK_ATTEMPT_ESCALATION_SUFFIXES
        ):
            return stage_name
    return None


def _reset_stage_work_attempts(
    db: HubDatabase,
    task_id: str,
    stage_name: str,
    reason: str,
) -> bool:
    row = db.fetchone(
        """
        SELECT *
          FROM task_stage_states
         WHERE task_id = %s AND stage_name = %s
        """,
        (task_id, stage_name),
    )
    if row is None:
        return False

    stage_state = row["state"]
    now = datetime.now(UTC).isoformat()
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE task_stage_states
               SET work_attempt_count = 0,
                   updated_at = %s
             WHERE task_id = %s AND stage_name = %s
            """,
            (now, task_id, stage_name),
        )
    TaskLifecycleEventManager(db).record_lifecycle_event(
        task_id,
        f"{stage_name}:{stage_state}",
        f"{stage_name}:{stage_state}",
        f"reset_stage_work_attempts:{reason}",
        by_actor="system",
    )
    return True


def submit_for_review(
    db: HubDatabase,
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
    db: HubDatabase,
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
    db: HubDatabase,
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
    db: HubDatabase,
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
    db: HubDatabase,
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
    )
    return get_task(db, task_id)
