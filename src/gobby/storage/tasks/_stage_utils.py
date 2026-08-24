"""Shared helpers for task stage-state transitions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from gobby.storage.delivery import upsert_merged_campaign_in_transaction
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.session_resolution import is_session_uuid
from gobby.storage.tasks._models import TaskHasOpenChildrenError, TaskStaleStateError
from gobby.utils.datetime import utc_now

_TERMINAL_PARENT_CLOSE_REASONS = frozenset({"completed", "obsolete"})


def _now() -> datetime:
    return utc_now()


def _session_exists(
    conn: HubDatabase | Transaction,
    session_id: str | None,
) -> bool:
    if not is_session_uuid(session_id):
        return False
    row = conn.execute("SELECT 1 FROM sessions WHERE id = %s", (session_id,)).fetchone()
    return row is not None


def _close_task_in_txn(
    conn: Transaction,
    task_id: str,
    *,
    db: HubDatabase | None = None,
    reason: str | None = None,
    commit_sha: str | None = None,
    closed_at: datetime | str | None = None,
    closed_in_session_id: str | None = None,
    force: bool = False,
    cascade_descendants: bool = False,
    close_ancestors: bool = True,
    closed_ancestors: list[str] | None = None,
    validation_override_reason: str | None = None,
    expected_updated_at: datetime | None = None,
    reset_validation_fail_count: bool = False,
    validation_status: str | None = None,
    validation_feedback: str | None = None,
) -> None:
    """Close a task inside the caller's already-open transaction."""

    if not force and not cascade_descendants:
        open_children = conn.execute(
            "SELECT id, title FROM tasks WHERE parent_task_id = %s AND closed_at IS NULL",
            (task_id,),
        ).fetchall()
        if open_children:
            listed = [f"{child['id']} ({child['title']})" for child in open_children[:3]]
            if len(open_children) > 3:
                listed.append(f"and {len(open_children) - 3} more")
            raise TaskHasOpenChildrenError(task_id, listed)

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
    # No is_escalated predicate here: every escalation bumps updated_at, so the
    # freshness guard already rejects closes racing a concurrent escalation.
    # A close whose caller read the escalated row (fresh updated_at, or no
    # expected_updated_at) is a deliberate resolution and clears escalation.
    cursor = conn.execute(
        """
        UPDATE tasks
           SET closed_at = %s,
               closed_reason = %s,
               closed_in_session_id = %s,
               closed_commit_sha = %s,
               validation_override_reason = %s,
               escalated_at = NULL,
               escalation_reason = NULL,
               is_escalated = FALSE,
               claimed_by_session_id = NULL,
               validation_fail_count = CASE WHEN %s THEN 0 ELSE validation_fail_count END,
               validation_status = COALESCE(%s, validation_status),
               validation_feedback = COALESCE(%s, validation_feedback),
               updated_at = %s
         WHERE id = %s
           AND closed_at IS NULL
           AND (%s::timestamptz IS NULL OR updated_at = %s)
        """,
        (
            now,
            reason,
            persisted_session_id,
            commit_sha,
            validation_override_reason,
            reset_validation_fail_count,
            validation_status,
            validation_feedback,
            now,
            task_id,
            expected_updated_at,
            expected_updated_at,
        ),
    )
    if cursor.rowcount == 0:
        current = conn.execute(
            "SELECT closed_at, updated_at FROM tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
        if current is None:
            raise ValueError(f"Task {task_id} not found")
        if expected_updated_at is not None and current["updated_at"] != expected_updated_at:
            raise TaskStaleStateError(task_id)
        if current["closed_at"] is not None:
            return
        raise TaskStaleStateError(task_id)
    if cascade_descendants:
        _cascade_close_descendants(conn, task_id, now, persisted_session_id, commit_sha)
    if close_ancestors:
        _close_eligible_ancestors(
            conn,
            task_id,
            db=db,
            reason=reason,
            closed_at=now,
            closed_in_session_id=persisted_session_id,
            closed_ancestors=closed_ancestors,
        )


def _close_eligible_ancestors(
    conn: Transaction,
    task_id: str,
    *,
    db: HubDatabase | None,
    reason: str | None,
    closed_at: datetime | str,
    closed_in_session_id: str | None,
    closed_ancestors: list[str] | None,
) -> None:
    """Close each ancestor that now has zero open children."""
    current_id = task_id
    close_reason = reason or "completed"
    while True:
        current = conn.execute(
            "SELECT parent_task_id FROM tasks WHERE id = %s",
            (current_id,),
        ).fetchone()
        parent_id = current["parent_task_id"] if current else None
        if not parent_id:
            return
        parent = conn.execute(
            """
            SELECT id, seq_num, title, task_type, project_id, closed_at
              FROM tasks
             WHERE id = %s
             FOR UPDATE
            """,
            (parent_id,),
        ).fetchone()
        if parent is None or parent["closed_at"] is not None:
            return
        open_child = conn.execute(
            """
            SELECT 1
              FROM tasks
             WHERE parent_task_id = %s
               AND closed_at IS NULL
             LIMIT 1
            """,
            (parent_id,),
        ).fetchone()
        if open_child is not None:
            return
        unfinished_stage = conn.execute(
            """
            SELECT 1
              FROM task_stage_states
             WHERE task_id = %s
               AND state != 'done'
             LIMIT 1
            """,
            (parent_id,),
        ).fetchone()
        if unfinished_stage is not None:
            # A stage manifest is work this parent still owes. Closing it on its
            # last child would ship the epic with its epic_qa, pr, and merge
            # stages never run, and strand those rows on a closed task — which
            # also makes it invisible to dispatch, since candidates require
            # closed_at IS NULL. Such a parent closes through its merge stage.
            # A parent with no manifest rows is unaffected.
            return
        _close_task_in_txn(
            conn,
            parent_id,
            db=db,
            reason=close_reason,
            closed_at=closed_at,
            closed_in_session_id=closed_in_session_id,
            close_ancestors=False,
        )
        if closed_ancestors is not None:
            closed_ancestors.append(parent_id)
        _schedule_ancestor_epic_archive(
            conn,
            db,
            task_id=parent_id,
            seq_num=parent["seq_num"],
            project_id=parent["project_id"],
            task_type=parent["task_type"],
            reason=close_reason,
        )
        current_id = parent_id


def _schedule_ancestor_epic_archive(
    conn: Transaction,
    db: HubDatabase | None,
    *,
    task_id: str,
    seq_num: int | None,
    project_id: str,
    task_type: str,
    reason: str,
) -> None:
    if db is None or task_type != "epic":
        return
    closure_reason = reason.casefold()
    if closure_reason not in _TERMINAL_PARENT_CLOSE_REASONS:
        return
    task_ref = f"#{seq_num}" if seq_num else task_id

    def _archive() -> None:
        from gobby.hooks.event_handlers._plan import on_epic_terminal

        on_epic_terminal(
            {
                "task_ref": task_ref,
                "project_id": project_id,
                "status": "closed",
                "closure_reason": closure_reason,
            },
            db=db,
        )

    conn.after_commit(_archive)


def _complete_terminal_delivery_stage_for_close(
    conn: Transaction,
    task_id: str,
    *,
    now: datetime | str,
    completed_by_session_id: str | None,
    commit_sha: str | None,
) -> None:
    row = conn.execute(
        """
        SELECT s.stage_name, s.state, r.category, r.is_terminal
          FROM task_stage_states s
          LEFT JOIN task_stages_registry r ON r.name = s.stage_name
         WHERE s.task_id = %s
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
    cursor = conn.execute(
        """
        UPDATE task_stage_states
           SET state = 'done',
               completed_at = COALESCE(completed_at, %s),
               completed_by_session_id = COALESCE(completed_by_session_id, %s),
               completed_by_actor = COALESCE(completed_by_actor, %s),
               completed_commit_sha = COALESCE(completed_commit_sha, %s),
               updated_at = %s
         WHERE task_id = %s
           AND stage_name = %s
           AND state = 'in_progress'
        """,
        (
            now,
            completed_by_session_id,
            "session" if completed_by_session_id else "system",
            completion_commit_sha,
            now,
            task_id,
            row["stage_name"],
        ),
    )
    if cursor.rowcount > 0 and row["stage_name"] == "merge":
        upsert_merged_campaign_in_transaction(
            conn,
            task_id,
            merge_sha=completion_commit_sha,
        )


def _is_terminal_delivery_stage(row: Any) -> bool:
    return bool(row["is_terminal"]) and row["category"] == "delivery"


def _completion_commit_sha_for_stage(
    conn: Transaction,
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
         WHERE task_id = %s
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
    conn: Transaction,
    task_id: str,
    closed_at: datetime | str,
    closed_in_session_id: str | None,
    commit_sha: str | None,
) -> None:
    conn.execute(
        """
        WITH RECURSIVE subtree(id, depth, path) AS (
            SELECT id, 1, ARRAY[parent_task_id, id]
              FROM tasks
             WHERE parent_task_id = %s
            UNION ALL
            SELECT tasks.id, subtree.depth + 1, subtree.path || tasks.id
              FROM tasks
              JOIN subtree ON tasks.parent_task_id = subtree.id
             WHERE subtree.depth < 100
               AND NOT tasks.id = ANY(subtree.path)
        )
        UPDATE tasks
           SET closed_at = %s,
               closed_reason = 'merged',
               closed_in_session_id = %s,
               closed_commit_sha = %s,
               claimed_by_session_id = NULL,
               updated_at = %s
         WHERE id IN (SELECT id FROM subtree)
           AND closed_at IS NULL
        """,
        (task_id, closed_at, closed_in_session_id, commit_sha, closed_at),
    )
