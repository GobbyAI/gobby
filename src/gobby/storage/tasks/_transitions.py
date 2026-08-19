"""Ownership-aware task transition helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

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
    TaskStaleStateError,
)
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._stage_utils import _close_task_in_txn
from gobby.storage.tasks._updates import update_task
from gobby.tasks.state_semantics import is_task_closed
from gobby.utils.datetime import parse_stored_datetime, utc_now


def _task_ref(task: Task, fallback: str) -> str:
    return f"#{task.seq_num}" if task.seq_num else fallback


def _has_active_dispatch_mutex(db: HubDatabase, task_id: str) -> bool:
    mutex = TaskDispatchMutexManager(db).get_mutex(task_id)
    if mutex is None:
        return False
    return mutex.lease_until is None or mutex.lease_until >= utc_now()


def _has_active_agent_run(db: HubDatabase, task_id: str) -> bool:
    return bool(LocalAgentRunManager(db).list_active_global(task_ids=[task_id], limit=1))


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


def release_task_claim_if_owned(
    db: HubDatabase,
    task_id: str,
    *,
    expected_owner: str,
) -> Task | None:
    """Clear ownership only when the task still has the expected live owner."""
    now = utc_now()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE tasks
               SET claimed_by_session_id = NULL,
                   updated_at = %s
             WHERE id = %s
               AND claimed_by_session_id = %s
               AND closed_at IS NULL
               AND escalated_at IS NULL
            """,
            (now, task_id, expected_owner),
        )
    return get_task(db, task_id) if cursor.rowcount == 1 else None


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
        # Named validation reset branch (d): manual de-escalation/reopen.
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
                   is_escalated = TRUE,
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


def escalate_task_if_owned(
    db: HubDatabase,
    task_id: str,
    *,
    reason: str,
    expected_owner: str,
) -> Task | None:
    """Escalate only when the task still has the expected live owner."""
    now = utc_now()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE tasks
               SET claimed_by_session_id = NULL,
                   escalated_at = %s,
                   escalation_reason = %s,
                   is_escalated = TRUE,
                   updated_at = %s
             WHERE id = %s
               AND claimed_by_session_id = %s
               AND closed_at IS NULL
               AND escalated_at IS NULL
            """,
            (now, reason, now, task_id, expected_owner),
        )
    return get_task(db, task_id) if cursor.rowcount == 1 else None


def increment_validation_failure(
    db: HubDatabase,
    task_id: str,
    *,
    expected_updated_at: datetime,
    threshold: int,
    validation_status: str,
    validation_feedback: str | None,
    escalation_reason: str,
) -> tuple[int, bool]:
    """Record one guarded invalid verdict and atomically escalate at the threshold."""
    if threshold <= 0:
        raise ValueError("Validation escalation threshold must be positive")

    now = utc_now()
    with db.transaction() as conn:
        row = conn.execute(
            """
            UPDATE tasks
               SET validation_fail_count = COALESCE(validation_fail_count, 0) + 1,
                   validation_status = %s,
                   validation_feedback = %s,
                   claimed_by_session_id = CASE
                       WHEN COALESCE(validation_fail_count, 0) + 1 >= %s THEN NULL
                       ELSE claimed_by_session_id
                   END,
                   escalated_at = CASE
                       WHEN COALESCE(validation_fail_count, 0) + 1 >= %s THEN %s
                       ELSE escalated_at
                   END,
                   escalation_reason = CASE
                       WHEN COALESCE(validation_fail_count, 0) + 1 >= %s THEN %s
                       ELSE escalation_reason
                   END,
                   is_escalated = CASE
                       WHEN COALESCE(validation_fail_count, 0) + 1 >= %s THEN TRUE
                       ELSE is_escalated
                   END,
                   updated_at = %s
             WHERE id = %s
               AND closed_at IS NULL
               AND is_escalated = FALSE
               AND updated_at = %s
             RETURNING validation_fail_count, is_escalated
            """,
            (
                validation_status,
                validation_feedback,
                threshold,
                threshold,
                now,
                threshold,
                escalation_reason,
                threshold,
                now,
                task_id,
                expected_updated_at,
            ),
        ).fetchone()

    if row is None:
        task = get_task(db, task_id)
        if is_task_closed(task):
            raise ValueError(
                f"Cannot record validation failure for task {task_id}: task is closed."
            )
        if task.is_escalated:
            raise TaskAlreadyEscalatedError(task_id, task.escalation_reason)
        raise TaskStaleStateError(task_id)
    return int(row["validation_fail_count"]), bool(row["is_escalated"])


def close_task(
    db: HubDatabase,
    task_id: str,
    *,
    reason: str | None = None,
    force: bool = False,
    closed_in_session_id: str | None = None,
    closed_commit_sha: str | None = None,
    closed_ancestors: list[str] | None = None,
    validation_override_reason: str | None = None,
    expected_updated_at: datetime | None = None,
    reset_validation_fail_count: bool = False,
    validation_status: str | None = None,
    validation_feedback: str | None = None,
) -> Task:
    """Close a task and clear active ownership metadata."""
    collected: list[str] = []
    with db.transaction() as conn:
        _close_task_in_txn(
            conn,
            task_id,
            db=db,
            reason=reason,
            commit_sha=closed_commit_sha,
            closed_in_session_id=closed_in_session_id,
            force=force,
            closed_ancestors=collected,
            validation_override_reason=validation_override_reason,
            expected_updated_at=expected_updated_at,
            reset_validation_fail_count=reset_validation_fail_count,
            validation_status=validation_status,
            validation_feedback=validation_feedback,
        )
    if closed_ancestors is not None:
        closed_ancestors.extend(collected)
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
    current = get_task(db, task_id)
    values = {
        "title": title,
        "description": description,
        "priority": priority,
        "closed_reason": closed_reason,
        "closed_at": closed_at,
        "closed_in_session_id": closed_in_session_id,
        "closed_commit_sha": closed_commit_sha,
        "escalated_at": escalated_at,
        "escalation_reason": escalation_reason,
    }
    timestamp_fields = {"closed_at", "escalated_at"}
    unchanged = True
    for field, value in values.items():
        if value is UNSET:
            continue
        current_value = getattr(current, field)
        if field in timestamp_fields:
            matches = parse_stored_datetime(current_value) == parse_stored_datetime(
                cast(datetime | str | None, value)
            )
        else:
            matches = current_value == value
        if not matches:
            unchanged = False
            break
    if unchanged:
        return current

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
