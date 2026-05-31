"""Task automation candidate and stale-claim helpers."""

from datetime import UTC, datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._ancestor_gate import find_child_development_ancestor_gate
from gobby.storage.tasks._blocking import hydrate_task_blocking_state
from gobby.storage.tasks._holistic_gate import find_holistic_descendant_gate
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._stage_hydration import hydrate_task_stage_state


def _is_unattended(task: Any) -> bool:
    """Return whether dispatch should avoid human escalation for a task."""
    return bool(getattr(task, "unattended", False))


def is_blocked_by_deps(task: object) -> bool:
    """Return whether a task has unresolved blocking dependencies."""
    active_blocked_by = getattr(task, "active_blocked_by", None)
    if active_blocked_by is not None:
        return bool(active_blocked_by)
    blocked_by = getattr(task, "blocked_by", None)
    return bool(blocked_by)


def list_automation_candidates(
    db: HubDatabase,
    *,
    project_id: str | None = None,
) -> list[Task]:
    """List unclaimed, unleased, dependency-ready tasks eligible for dispatch."""
    now = datetime.now(UTC).isoformat()
    params: list[Any] = [now]
    project_filter = ""
    if project_id is not None:
        project_filter = "AND tasks.project_id = %s"
        params.append(project_id)

    rows = db.fetchall(
        f"""
        SELECT tasks.*
        FROM tasks
        JOIN task_stage_states current_stage
          ON current_stage.task_id = tasks.id
         AND current_stage.state != 'done'
         AND current_stage.position = (
             SELECT MIN(stage_scan.position)
               FROM task_stage_states stage_scan
              WHERE stage_scan.task_id = tasks.id
                AND stage_scan.state != 'done'
         )
        LEFT JOIN task_dispatch_mutex mutex ON mutex.task_id = tasks.id
        WHERE tasks.allow_automation IS TRUE
          AND tasks.claimed_by_session_id IS NULL
          AND tasks.closed_at IS NULL
          AND tasks.escalated_at IS NULL
          AND COALESCE(tasks.is_escalated, FALSE) IS FALSE
          AND current_stage.state IN ('ready', 'in_progress', 'needs_review', 'review_approved')
          AND (
              mutex.task_id IS NULL
              OR mutex.lease_until IS NULL
              OR mutex.lease_until < %s
          )
          {project_filter}
        ORDER BY tasks.priority ASC, tasks.seq_num ASC, tasks.created_at ASC
        """,  # nosec B608 # project_filter is static SQL selected above.
        tuple(params),
    )
    tasks = [Task.from_row(row) for row in rows]
    hydrate_task_stage_state(db, tasks)
    hydrate_task_blocking_state(db, tasks)
    ready_tasks = [
        task
        for task in tasks
        if not is_blocked_by_deps(task) and find_child_development_ancestor_gate(db, task) is None
    ]
    holistic_gate_by_task_id = {
        task.id: find_holistic_descendant_gate(db, task) for task in ready_tasks
    }
    return sorted(
        ready_tasks,
        key=lambda task: (
            holistic_gate_by_task_id[task.id] is None,
            task.priority,
            task.seq_num if task.seq_num is not None else 2**31,
            task.created_at,
        ),
    )


def sweep_stale_claims(
    db: HubDatabase,
    *,
    project_id: str | None = None,
) -> int:
    """Release task claims held by sessions that are no longer active."""
    now = datetime.now(UTC).isoformat()
    params: list[Any] = [now]
    project_filter = ""
    if project_id is not None:
        project_filter = "AND project_id = %s"
        params.append(project_id)

    with db.transaction() as conn:
        cursor = conn.execute(
            f"""
            UPDATE tasks
               SET claimed_by_session_id = NULL,
                   assignee = NULL,
                   updated_at = %s
             WHERE allow_automation IS TRUE
               AND closed_at IS NULL
               AND escalated_at IS NULL
               AND COALESCE(is_escalated, FALSE) IS FALSE
               AND claimed_by_session_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM sessions s
                    WHERE s.id = tasks.claimed_by_session_id
                      AND s.status IN ('active', 'paused')
               )
               {project_filter}
            """,  # nosec B608 # project_filter is static SQL selected above.
            tuple(params),
        )
        return cursor.rowcount
