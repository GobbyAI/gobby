"""Durable task delegation to another live session."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._constants import LIVE_SESSION_STATUSES
from gobby.storage.tasks._models import Task


def delegate_task(
    db: HubDatabase,
    task_id: str,
    *,
    delegated_by_session_id: str,
    delegated_to_session_id: str,
    reason: str,
) -> Task:
    """Record one current delegation, checking ownership and liveness atomically."""
    reason = reason.strip()
    if not reason:
        raise ValueError("Delegation reason is required")
    if delegated_by_session_id == delegated_to_session_id:
        raise ValueError("Cannot delegate a task to self")

    with db.transaction() as conn:
        row = conn.execute(
            """
            SELECT t.created_in_session_id, t.project_id, t.claimed_by_session_id,
                   t.closed_at, s.status AS target_status, s.project_id AS target_project_id
            FROM tasks t
            LEFT JOIN sessions s ON s.id = %s
            WHERE t.id = %s
            FOR UPDATE OF t
            """,
            (delegated_to_session_id, task_id),
        ).fetchone()
        if row is None:
            raise ValueError("Task not found")
        if str(row["created_in_session_id"]) != delegated_by_session_id:
            raise ValueError("Only the session that filed the task may delegate it")
        if row["closed_at"] is not None or row["claimed_by_session_id"] is not None:
            raise ValueError("Only an open, unclaimed task may be delegated")
        if row["target_status"] not in LIVE_SESSION_STATUSES:
            raise ValueError("Delegation requires a live receiving session")
        if row["target_project_id"] != row["project_id"]:
            raise ValueError("The receiving session must belong to the task project")

        updated = conn.execute(
            """
            UPDATE tasks
            SET delegated_to_session_id = %s, delegated_by_session_id = %s,
                delegation_reason = %s, delegated_at = now(), updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (delegated_to_session_id, delegated_by_session_id, reason, task_id),
        ).fetchone()
        if updated is None:
            raise ValueError("Task not found")
        return Task.from_row(updated)
