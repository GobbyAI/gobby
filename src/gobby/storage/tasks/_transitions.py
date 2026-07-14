"""Ownership-aware task transition helpers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from gobby.plans.bootstrap_ledger import bootstrap_ledger_path_for_task, verify_bootstrap_ledger
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._artifacts import get_artifacts, set_artifacts_atomic
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
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.tasks._stage_types import NoCurrentStageError
from gobby.storage.tasks._stage_utils import _close_task_in_txn
from gobby.storage.tasks._updates import update_task
from gobby.tasks.state_semantics import is_task_closed
from gobby.utils.datetime import utc_now

_WORK_ATTEMPT_ESCALATION_SUFFIXES = ("_work_failed:max", "_max_work_attempts")


def _stage_states(db: HubDatabase) -> StageStatesManager:
    return StageStatesManager(db, TaskLifecycleEventManager(db))


def _task_ref(task: Task, fallback: str) -> str:
    return f"#{task.seq_num}" if task.seq_num else fallback


def _has_active_dispatch_mutex(db: HubDatabase, task_id: str) -> bool:
    mutex = TaskDispatchMutexManager(db).get_mutex(task_id)
    if mutex is None:
        return False
    return mutex.lease_until is None or mutex.lease_until >= utc_now()


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

    now = utc_now()
    stage_name = row["stage_name"]
    from_state = row["state"]
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE task_stage_states
               SET state = 'ready',
                   entered_at = NULL,
                   entered_by_session_id = NULL,
                   entered_by_actor = NULL,
                   completed_at = NULL,
                   completed_by_session_id = NULL,
                   completed_by_actor = NULL,
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


def get_effective_claim_owner(task: Task) -> str | None:
    """Return the owning session for a task."""
    return task.claimed_by_session_id


def claim_task(
    db: HubDatabase,
    task_id: str,
    session_id: str,
    *,
    force: bool = False,
    expected_owner: str | None = None,
) -> Task:
    """Claim a task for a session with an atomic ownership guard."""
    if force and expected_owner is not None:
        raise ValueError("force and expected_owner are mutually exclusive")
    if is_task_closed(get_task(db, task_id)):
        raise TaskClosedError(f"Cannot claim task {task_id}: task is closed")

    params: tuple[Any, ...]
    if force:
        sql = """
            UPDATE tasks
            SET claimed_by_session_id = %s, updated_at = %s
            WHERE id = %s AND closed_at IS NULL
        """
        params = (session_id, utc_now(), task_id)
    else:
        permitted_owners = [session_id]
        if expected_owner is not None:
            permitted_owners.append(expected_owner)
        sql = """
            UPDATE tasks
            SET claimed_by_session_id = %s, updated_at = %s
            WHERE id = %s
              AND closed_at IS NULL
              AND (
                  claimed_by_session_id IS NULL
                  OR claimed_by_session_id = ANY(%s::uuid[])
              )
        """
        params = (
            session_id,
            utc_now(),
            task_id,
            permitted_owners,
        )
    with db.transaction() as conn:
        cursor = conn.execute(sql, params)

    if cursor.rowcount == 0:
        task = get_task(db, task_id)
        if is_task_closed(task):
            raise TaskClosedError(f"Cannot claim task {task_id}: task is closed")
        current_owner = get_effective_claim_owner(task)
        if current_owner is None:
            raise RuntimeError(f"Task {task_id} claim failed without a conflicting owner")
        raise TaskAlreadyClaimedError(task_id, current_owner)

    return get_task(db, task_id)


def release_task_claim(
    db: HubDatabase,
    task_id: str,
    *,
    description: MaybeUnset[str | None] = UNSET,
    validation_fail_count: MaybeUnset[int | None] = UNSET,
    dispatch_failure_count: MaybeUnset[int | None] = UNSET,
    escalated_at: MaybeUnset[datetime | str | None] = UNSET,
    escalation_reason: MaybeUnset[str | None] = UNSET,
    validation_override_reason: MaybeUnset[str | None] = UNSET,
    labels: MaybeUnset[list[str] | None] = UNSET,
) -> Task:
    """Clear ownership while optionally changing recovery metadata."""
    update_task(
        db,
        task_id,
        description=description,
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
    has_ownership_metadata = bool(task.claimed_by_session_id)
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
    now = utc_now()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE tasks
               SET claimed_by_session_id = NULL,
                   escalated_at = %s,
                   escalation_reason = %s,
                   validation_override_reason = COALESCE(%s, validation_override_reason),
                   updated_at = %s
             WHERE id = %s
               AND closed_at IS NULL
               AND escalated_at IS NULL
            """,
            (now, reason, validation_override_reason, now, task_id),
        )

    if cursor.rowcount == 0:
        task = get_task(db, task_id)
        if task.is_escalated:
            raise TaskAlreadyEscalatedError(task_id, task.escalation_reason)
        if is_task_closed(task):
            raise ValueError(f"Cannot escalate task {task_id}: task is closed.")
        raise RuntimeError(f"Task {task_id} escalation failed without a conflicting transition")

    return get_task(db, task_id)


def de_escalate_task(
    db: HubDatabase,
    task_id: str,
    *,
    reason: str,
    reset_validation: bool = False,
    reset_stage_attempts: bool = False,
    restore_stage_from_history: bool = False,
) -> Task:
    """Clear escalation state and keep the current stage unchanged."""
    task = get_task(db, task_id)
    if not task.is_escalated:
        raise ValueError(f"Task {task_id} is not escalated")

    if restore_stage_from_history:
        _restore_stage_from_history_for_de_escalation(
            db,
            task_id,
            reason=reason,
            escalation_reason=task.escalation_reason,
        )

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


def _restore_stage_from_history_for_de_escalation(
    db: HubDatabase,
    task_id: str,
    *,
    reason: str,
    escalation_reason: str | None,
) -> None:
    current = _current_stage_row(db, task_id)
    if current is None:
        raise ValueError(f"Cannot restore task {task_id} stage from history: no current stage")

    current_stage_name = str(current["stage_name"])
    stage_name = _stage_name_from_work_attempt_escalation(db, task_id, escalation_reason)
    if stage_name is not None and stage_name != current_stage_name:
        raise ValueError(
            "Cannot restore task "
            f"{task_id} stage from history: escalated stage {stage_name!r} is not "
            f"the current stage {current_stage_name!r}"
        )
    stage_name = stage_name or current_stage_name

    current_state = str(current["state"])
    if current_state != "ready":
        raise ValueError(
            "Cannot restore task "
            f"{task_id} stage {stage_name!r} from history: current state is "
            f"{current_state!r}, expected 'ready'"
        )

    restored_state = "review_approved"
    history = db.fetchone(
        """
        SELECT 1
          FROM task_lifecycle_events
         WHERE task_id = %s
           AND from_state = %s
           AND to_state = %s
           AND reason = 'build_stop'
         ORDER BY id DESC
         LIMIT 1
        """,
        (task_id, f"{stage_name}:{restored_state}", f"{stage_name}:ready"),
    )
    if history is None:
        raise ValueError(
            "Cannot restore task "
            f"{task_id} stage {stage_name!r} from history: no build_stop "
            f"{restored_state!r} to 'ready' lifecycle event was found"
        )

    now = utc_now()
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE task_stage_states
               SET state = %s,
                   updated_at = %s
             WHERE task_id = %s AND stage_name = %s
            """,
            (restored_state, now, task_id, stage_name),
        )
    TaskLifecycleEventManager(db).record_lifecycle_event(
        task_id,
        f"{stage_name}:{current_state}",
        f"{stage_name}:{restored_state}",
        f"restore_stage_from_history:{reason}",
        by_actor="system",
    )


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
    now = utc_now()
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
    dispatch_run_id: str | None = None,
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
        dispatch_run_id=dispatch_run_id,
    )
    description: MaybeUnset[str | None] = UNSET
    if review_notes:
        description = (task.description or "") + f"\n\n[Review Notes]\n{review_notes}"
    update_task(
        db,
        task_id,
        description=description,
        claimed_by_session_id=None,
    )
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE tasks
               SET labels = COALESCE(labels, '[]'::jsonb)
                            - 'planning-current-verdict:rejected',
                   updated_at = %s
             WHERE id = %s
               AND COALESCE(labels, '[]'::jsonb)
                   @> '["planning-current-verdict:rejected"]'::jsonb
            """,
            (utc_now(), task_id),
        )
    return get_task(db, task_id)


def approve_review(
    db: HubDatabase,
    task_id: str,
    stage_name: str | None = None,
    *,
    approval_notes: str | None = None,
    by_session_id: str | None = None,
    dispatch_run_id: str | None = None,
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
        dispatch_run_id=dispatch_run_id,
    )
    description: MaybeUnset[str | None] = UNSET
    if approval_notes:
        description = (task.description or "") + f"\n\n[Approval Notes]\n{approval_notes}"

    update_task(
        db,
        task_id,
        description=description,
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
    dispatch_run_id: str | None = None,
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
        dispatch_run_id=dispatch_run_id,
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
        claimed_by_session_id=None,
    )
    return get_task(db, task_id)


def _enhancement_section_body(suggestions: Sequence[str], *, converged: bool) -> str:
    if suggestions:
        return "\n".join(f"- {item}" for item in suggestions)
    note = "Converged; no further suggestions." if converged else "No suggestions this round."
    return f"_{note}_"


def _fold_enhancement_round(
    existing: str,
    round_number: int,
    suggestions: Sequence[str],
    *,
    converged: bool,
) -> str:
    """Idempotently fold a round's enhancement section into the description.

    Re-running the same round replaces that round's section in place rather than
    stacking a duplicate heading, mirroring the adversary-round dedup behavior.
    """
    heading = f"## Enhancement Suggestions — Round {round_number}"
    section = f"{heading}\n\n{_enhancement_section_body(suggestions, converged=converged)}"
    if heading in existing:
        import re

        pattern = re.compile(
            rf"^{re.escape(heading)}.*?(?=^## Enhancement Suggestions — Round |\Z)",
            re.DOTALL | re.MULTILINE,
        )
        return pattern.sub(section.rstrip() + "\n\n", existing).rstrip() or section
    return f"{existing}\n\n{section}" if existing else section


def record_plan_enhancement(
    db: HubDatabase,
    task_id: str,
    *,
    round_number: int,
    converged: bool,
    suggestions: Sequence[str] | None = None,
    signoff_summary: str | None = None,
    by_session_id: str | None = None,
) -> Task:
    """Record a constructive plan-enhancement round on the planning stage.

    Enhancement is tracked independently of the adversary review budget. When
    ``suggestions`` are present the planning stage returns from ``needs_review``
    to ``ready`` so the planner folds them in, WITHOUT incrementing
    ``review_round_count``. When the round converges or yields no suggestions the
    stage stays in ``needs_review`` so the adversary gate proceeds. Either way the
    enhancement counters are persisted and the claim is released.
    """
    normalized_round = int(round_number)
    if normalized_round < 1:
        raise ValueError("round_number must be >= 1")

    task = get_task(db, task_id)
    stages = _stage_states(db)
    current = stages.current_stage(task_id)
    if current is None:
        raise NoCurrentStageError(task_id)
    if current.stage_name != "planning":
        raise ValueError(
            "record_plan_enhancement requires the planning stage to be current; "
            f"current stage is '{current.stage_name}'"
        )
    if current.state != "needs_review":
        raise ValueError(
            "record_plan_enhancement requires the planning stage to be in needs_review; "
            f"current state is '{current.state}'"
        )

    cleaned = [item.strip() for item in (suggestions or []) if item and item.strip()]
    has_suggestions = bool(cleaned)

    # Enhancement counters live in task_artifacts, independent of review rounds.
    artifacts = get_artifacts(db, task_id)
    set_artifacts_atomic(
        db,
        task_id,
        plan_enhancement_rounds_completed=max(
            artifacts.plan_enhancement_rounds_completed, normalized_round
        ),
        plan_enhancement_converged=converged,
    )

    description = _fold_enhancement_round(
        task.description or "",
        normalized_round,
        cleaned,
        converged=converged,
    )

    if has_suggestions:
        # Route the plan back to the planner; route_enhancement intentionally
        # does NOT bump review_round_count (only reject_review does).
        stages.route_enhancement(
            task_id,
            current.stage_name,
            by_session_id=by_session_id,
            notes=signoff_summary,
        )

    update_task(
        db,
        task_id,
        description=description,
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
    closed_reason: MaybeUnset[str | None] = UNSET,
    closed_at: MaybeUnset[str | None] = UNSET,
    closed_in_session_id: MaybeUnset[str | None] = UNSET,
    closed_commit_sha: MaybeUnset[str | None] = UNSET,
    escalated_at: MaybeUnset[datetime | str | None] = UNSET,
    escalation_reason: MaybeUnset[str | None] = UNSET,
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
        closed_reason=closed_reason,
        closed_at=closed_at,
        closed_in_session_id=closed_in_session_id,
        closed_commit_sha=closed_commit_sha,
        escalated_at=escalated_at,
        escalation_reason=escalation_reason,
    )
    return get_task(db, task_id)
