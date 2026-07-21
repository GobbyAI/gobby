"""Task de-escalation transitions and stage recovery helpers."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import UNSET, Task
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._transitions import _current_stage_row, release_task_claim
from gobby.utils.datetime import utc_now

_WORK_ATTEMPT_ESCALATION_SUFFIXES = ("_work_failed:max", "_max_work_attempts")


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

    description = (
        f"{task.description}\n\nDe-escalated: {reason}"
        if task.description
        else f"De-escalated: {reason}"
    )

    with db.transaction():
        if restore_stage_from_history:
            _restore_stage_from_history_for_de_escalation(
                db,
                task_id,
                reason=reason,
                escalation_reason=task.escalation_reason,
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
        SELECT from_state, reason
          FROM task_lifecycle_events
         WHERE task_id = %s
           AND to_state = %s
         ORDER BY id DESC
         LIMIT 1
        """,
        (task_id, f"{stage_name}:ready"),
    )
    expected_from_state = f"{stage_name}:{restored_state}"
    if (
        history is None
        or history["from_state"] != expected_from_state
        or history["reason"] != "build_stop"
    ):
        raise ValueError(
            "Cannot restore task "
            f"{task_id} stage {stage_name!r} from history: latest transition into "
            f"'ready' was not a build_stop {restored_state!r} to 'ready' event"
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
