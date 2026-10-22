"""Durable task delegation to another live session."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._constants import LIVE_SESSION_STATUSES, TERMINAL_SESSION_STATUSES
from gobby.storage.tasks._models import Task


def delegate_task(
    db: HubDatabase,
    task_id: str,
    *,
    delegated_by_session_id: str,
    delegated_to_session_id: str,
    reason: str,
) -> Task:
    """Record a filer or ended-filer receiver delegation under the task row lock."""
    reason = reason.strip()
    if not reason:
        raise ValueError("Delegation reason is required")
    if delegated_by_session_id == delegated_to_session_id:
        raise ValueError("Cannot delegate a task to self")

    with db.transaction() as conn:
        row = conn.execute(
            """
            SELECT t.created_in_session_id, t.project_id, t.claimed_by_session_id,
                   t.closed_at, t.delegated_to_session_id,
                   filer.status AS filer_status,
                   actor.status AS actor_status, actor.project_id AS actor_project_id,
                   target.status AS target_status, target.project_id AS target_project_id
            FROM tasks t
            LEFT JOIN sessions filer ON filer.id = t.created_in_session_id
            LEFT JOIN sessions actor ON actor.id = %s
            LEFT JOIN sessions target ON target.id = %s
            WHERE t.id = %s
            FOR UPDATE OF t
            """,
            (delegated_by_session_id, delegated_to_session_id, task_id),
        ).fetchone()
        if row is None:
            raise ValueError("Task not found")
        is_filer = str(row["created_in_session_id"]) == delegated_by_session_id
        is_current_receiver_after_filer_ended = (
            str(row["delegated_to_session_id"]) == delegated_by_session_id
            and (
                row["filer_status"] in TERMINAL_SESSION_STATUSES or row["filer_status"] == "closed"
            )
            and row["actor_status"] in LIVE_SESSION_STATUSES
            and row["actor_project_id"] == row["project_id"]
        )
        if not (is_filer or is_current_receiver_after_filer_ended):
            raise ValueError(
                "Only the session that filed the task or its current receiver after the filer "
                "ended may delegate it"
            )
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
