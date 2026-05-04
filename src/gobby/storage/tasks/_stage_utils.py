"""Shared helpers for task stage-state transitions."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from gobby.plans.bootstrap_ledger import bootstrap_ledger_path_for_task, verify_bootstrap_ledger
from gobby.storage.database import DatabaseProtocol


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _session_exists(conn: sqlite3.Connection, session_id: str | None) -> bool:
    if not session_id:
        return False
    row = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row is not None


def _close_task_in_txn(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    db: DatabaseProtocol | None = None,
    reason: str | None = None,
    commit_sha: str | None = None,
    closed_at: str | None = None,
    closed_in_session_id: str | None = None,
    force: bool = False,
    cascade_descendants: bool = False,
    validation_override_reason: str | None = None,
) -> None:
    """Close a task inside the caller's already-open transaction."""

    if not force and not cascade_descendants:
        open_children = conn.execute(
            "SELECT id, title FROM tasks WHERE parent_task_id = ? AND closed_at IS NULL",
            (task_id,),
        ).fetchall()
        if open_children:
            child_list = ", ".join(
                f"{child['id']} ({child['title']})" for child in open_children[:3]
            )
            if len(open_children) > 3:
                child_list += f" and {len(open_children) - 3} more"
            raise ValueError(
                f"Cannot close task {task_id}: has {len(open_children)} open child task(s): "
                f"{child_list}"
            )

    if db is not None and bootstrap_ledger_path_for_task(db, task_id) is not None:
        verify_bootstrap_ledger(db, task_id)

    now = closed_at or _now()
    persisted_session_id = (
        closed_in_session_id if _session_exists(conn, closed_in_session_id) else None
    )
    conn.execute(
        """
        UPDATE tasks
           SET closed_at = ?,
               closed_reason = ?,
               closed_in_session_id = ?,
               closed_commit_sha = ?,
               validation_override_reason = ?,
               escalated_at = NULL,
               escalation_reason = NULL,
               is_escalated = 0,
               assignee = NULL,
               claimed_by_session_id = NULL,
               updated_at = ?
         WHERE id = ?
        """,
        (
            now,
            reason,
            persisted_session_id,
            commit_sha,
            validation_override_reason,
            now,
            task_id,
        ),
    )
    if cascade_descendants:
        _cascade_close_descendants(conn, task_id, now, persisted_session_id, commit_sha)


def _cascade_close_descendants(
    conn: sqlite3.Connection,
    task_id: str,
    closed_at: str,
    closed_in_session_id: str | None,
    commit_sha: str | None,
) -> None:
    rows = conn.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM tasks WHERE parent_task_id = ?
            UNION ALL
            SELECT tasks.id FROM tasks JOIN subtree ON tasks.parent_task_id = subtree.id
        )
        SELECT id FROM subtree
        """,
        (task_id,),
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            UPDATE tasks
               SET closed_at = ?,
                   closed_reason = 'merged',
                   closed_in_session_id = ?,
                   closed_commit_sha = ?,
                   assignee = NULL,
                   claimed_by_session_id = NULL,
                   updated_at = ?
             WHERE id = ?
            """,
            (closed_at, closed_in_session_id, commit_sha, closed_at, row["id"]),
        )
