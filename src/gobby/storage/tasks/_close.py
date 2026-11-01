"""Atomic task closure and eligible ancestor propagation."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import JsonValue

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.session_resolution import is_session_uuid
from gobby.storage.tasks._models import (
    TaskStaleStateError,
)
from gobby.tasks.state_semantics import validate_task_close_reason
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    pass


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
    db: HubDatabase,
    reason: str | None = None,
    commit_sha: str | None = None,
    closed_at: datetime | str | None = None,
    closed_in_session_id: str | None = None,
    close_ancestors: bool = True,
    closed_ancestors: list[str] | None = None,
    expected_updated_at: datetime | None = None,
    expected_closure_revision: str | None = None,
    closure_facts: dict[str, JsonValue] | None = None,
    prospective_commits: list[str] | None = None,
    closing_note: str | None = None,
    closure_intent: str = "explicit",
) -> None:
    """Close under the selected policy in the caller's already-open transaction."""
    reason = validate_task_close_reason("done" if reason is None else reason)

    # Serialize policy capture with evidence and explicit parent-attachment locks.
    # Keys do not change: NO KEY UPDATE permits the FK KEY SHARE locks held by
    # sibling writers, avoiding a deadlock when their closes cascade to this parent.
    row = conn.execute("SELECT * FROM tasks WHERE id = %s FOR NO KEY UPDATE", (task_id,)).fetchone()
    if row is not None and row["closed_at"] is None:
        from gobby.tasks.closure_snapshot import TaskClosurePolicyError, prepare_closure

        prepared = prepare_closure(
            db,
            task_id=task_id,
            session_id=closed_in_session_id,
            reason=reason,
            expected_updated_at=expected_updated_at,
            facts={**(closure_facts or {}), "closure_intent": closure_intent},
            prospective_commits=prospective_commits,
        )
        if expected_closure_revision is not None and (
            prepared.decision.snapshot_revision != expected_closure_revision
        ):
            raise TaskStaleStateError(task_id)
        if not prepared.decision.allowed:
            raise TaskClosurePolicyError(task_id, prepared.decision)

    now = closed_at or utc_now()
    persisted_session_id = (
        closed_in_session_id if _session_exists(conn, closed_in_session_id) else None
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
               escalated_at = NULL,
               escalation_reason = NULL,
               is_escalated = FALSE,
               claimed_by_session_id = NULL,
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
    if closing_note is not None:
        # The closing record: written in the same transaction as the close it describes.
        conn.execute(
            """
            INSERT INTO task_lifecycle_events (task_id, from_state, to_state, reason, by_actor)
            VALUES (%s, NULL, 'closed', %s, %s)
            """,
            (task_id, closing_note, persisted_session_id or "close_task"),
        )
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
    db: HubDatabase,
    reason: str | None,
    closed_at: datetime | str,
    closed_in_session_id: str | None,
    closed_ancestors: list[str] | None,
) -> None:
    """Close each ancestor that now has zero open children."""
    current = conn.execute(
        "SELECT parent_task_id FROM tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    close_eligible_parent_chain(
        conn,
        current["parent_task_id"] if current else None,
        db=db,
        reason=reason,
        closed_at=closed_at,
        closed_in_session_id=closed_in_session_id,
        closed_ancestors=closed_ancestors,
    )


def close_eligible_parent_chain(
    conn: Transaction,
    parent_id: str | None,
    *,
    db: HubDatabase,
    reason: str | None,
    closed_at: datetime | str,
    closed_in_session_id: str | None,
    closed_ancestors: list[str] | None,
) -> None:
    """Close ``parent_id``, then each ancestor above it, while each has zero open children.

    A child's close enters at its parent; a reparent enters at the old parent, which
    may have just lost its last open child (#23384).
    """
    close_reason = validate_task_close_reason("done" if reason is None else reason)
    while parent_id:
        parent = conn.execute(
            "SELECT * FROM tasks WHERE id = %s FOR NO KEY UPDATE", (parent_id,)
        ).fetchone()
        if parent is None or parent["closed_at"] is not None:
            return
        from gobby.tasks.closure_snapshot import TaskClosurePolicyError

        # Every ancestor uses the same evaluator as an explicit close. Its own
        # policy may require evidence or reserve it for a separate close request.
        try:
            _close_task_in_txn(
                conn,
                parent_id,
                db=db,
                reason=close_reason,
                closed_at=closed_at,
                closed_in_session_id=closed_in_session_id,
                close_ancestors=False,
                closure_intent="ancestor",
            )
        except (TaskClosurePolicyError, TaskStaleStateError):
            return
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
        parent_id = parent["parent_task_id"]


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
