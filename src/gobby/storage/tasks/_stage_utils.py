"""Shared helpers for task stage-state transitions."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from gobby.plans.bootstrap_ledger import bootstrap_ledger_path_for_task, verify_bootstrap_ledger
from gobby.storage.hub.protocol import HubDatabase, Transaction


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _session_exists(
    conn: HubDatabase | Transaction | sqlite3.Connection,
    session_id: str | None,
) -> bool:
    if not session_id:
        return False
    row = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row is not None


def _close_task_in_txn(
    conn: Transaction | sqlite3.Connection,
    task_id: str,
    *,
    db: HubDatabase | None = None,
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
    _complete_terminal_delivery_stage_for_close(
        conn,
        task_id,
        now=now,
        completed_by_session_id=persisted_session_id,
        commit_sha=commit_sha,
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


def _complete_terminal_delivery_stage_for_close(
    conn: Transaction | sqlite3.Connection,
    task_id: str,
    *,
    now: str,
    completed_by_session_id: str | None,
    commit_sha: str | None,
) -> None:
    row = conn.execute(
        """
        SELECT s.stage_name, s.state, r.category, r.is_terminal
          FROM task_stage_states s
          LEFT JOIN task_stages_registry r ON r.name = s.stage_name
         WHERE s.task_id = ?
           AND s.state != 'done'
         ORDER BY s.position, s.stage_name
         LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is None or row["state"] != "in_progress":
        return
    if not _is_terminal_delivery_stage(row):
        return

    completion_commit_sha = _completion_commit_sha_for_stage(
        conn,
        task_id,
        row["stage_name"],
        commit_sha,
    )
    conn.execute(
        """
        UPDATE task_stage_states
           SET state = 'done',
               completed_at = COALESCE(completed_at, ?),
               completed_by_session_id = COALESCE(completed_by_session_id, ?),
               completed_commit_sha = COALESCE(completed_commit_sha, ?),
               updated_at = ?
         WHERE task_id = ?
           AND stage_name = ?
           AND state = 'in_progress'
        """,
        (
            now,
            completed_by_session_id,
            completion_commit_sha,
            now,
            task_id,
            row["stage_name"],
        ),
    )


def _is_terminal_delivery_stage(row: Any) -> bool:
    return bool(row["is_terminal"]) and row["category"] == "delivery"


def _completion_commit_sha_for_stage(
    conn: Transaction | sqlite3.Connection,
    task_id: str,
    stage_name: str,
    fallback_commit_sha: str | None,
) -> str | None:
    if stage_name != "merge":
        return fallback_commit_sha
    row = conn.execute(
        """
        SELECT merge_sha
          FROM task_delivery_campaigns
         WHERE task_id = ?
           AND state = 'merged'
           AND merge_sha IS NOT NULL
           AND merge_sha != ''
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return fallback_commit_sha
    return row["merge_sha"] or fallback_commit_sha


def _cascade_close_descendants(
    conn: Transaction | sqlite3.Connection,
    task_id: str,
    closed_at: str,
    closed_in_session_id: str | None,
    commit_sha: str | None,
) -> None:
    conn.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM tasks WHERE parent_task_id = ?
            UNION ALL
            SELECT tasks.id FROM tasks JOIN subtree ON tasks.parent_task_id = subtree.id
        )
        UPDATE tasks
           SET closed_at = ?,
               closed_reason = 'merged',
               closed_in_session_id = ?,
               closed_commit_sha = ?,
               assignee = NULL,
               claimed_by_session_id = NULL,
               updated_at = ?
         WHERE id IN (SELECT id FROM subtree)
        """,
        (task_id, closed_at, closed_in_session_id, commit_sha, closed_at),
    )
