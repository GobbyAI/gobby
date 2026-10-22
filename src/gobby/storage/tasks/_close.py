"""Atomic task closure and eligible ancestor propagation."""

from __future__ import annotations

from datetime import datetime

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.session_resolution import is_session_uuid
from gobby.storage.tasks._automation import has_hold_label
from gobby.storage.tasks._models import TaskHasOpenChildrenError, TaskStaleStateError
from gobby.tasks.criteria_contract import normalized_validation_criteria
from gobby.utils.datetime import utc_now

_LEGACY_VALIDATION_CRITERIA = (
    "Legacy task: validation criteria were not recorded before they became required."
)
_TERMINAL_PARENT_CLOSE_REASONS = frozenset({"completed", "obsolete"})


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
    close_ancestors: bool = True,
    closed_ancestors: list[str] | None = None,
    validation_override_reason: str | None = None,
    expected_updated_at: datetime | None = None,
    reset_validation_fail_count: bool = False,
    validation_status: str | None = None,
    validation_feedback: str | None = None,
) -> None:
    """Close a task inside the caller's already-open transaction."""

    if not force:
        # Hold this row before counting children. Creation takes the same lock on
        # the parent it is attaching to, so the two serialize here; without it the
        # count runs before the UPDATE acquires any lock, and a child inserted in
        # that window survives its parent's close (#22570). _close_eligible_ancestors
        # already locks an ancestor this way before its own open-child probe.
        conn.execute("SELECT 1 FROM tasks WHERE id = %s FOR UPDATE", (task_id,))
        open_children = conn.execute(
            "SELECT id, title FROM tasks WHERE parent_task_id = %s AND closed_at IS NULL",
            (task_id,),
        ).fetchall()
        if open_children:
            listed = [f"{child['id']} ({child['title']})" for child in open_children[:3]]
            if len(open_children) > 3:
                listed.append(f"and {len(open_children) - 3} more")
            raise TaskHasOpenChildrenError(task_id, listed)

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
               validation_override_reason = %s,
               escalated_at = NULL,
               escalation_reason = NULL,
               is_escalated = FALSE,
               claimed_by_session_id = NULL,
               validation_fail_count = CASE WHEN %s THEN 0 ELSE validation_fail_count END,
               validation_status = COALESCE(%s, validation_status),
               validation_feedback = COALESCE(%s, validation_feedback),
               validation_criteria = CASE
                   WHEN task_type = 'epic'
                        OR NULLIF(btrim(validation_criteria), '') IS NOT NULL
                   THEN validation_criteria
                   ELSE %s
               END,
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
            _LEGACY_VALIDATION_CRITERIA,
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
    db: HubDatabase | None,
    reason: str | None,
    closed_at: datetime | str,
    closed_in_session_id: str | None,
    closed_ancestors: list[str] | None,
) -> None:
    """Close ``parent_id``, then each ancestor above it, while each has zero open children.

    A child's close enters at its parent; a reparent enters at the old parent, which
    may have just lost its last open child (#23384).
    """
    close_reason = reason or "completed"
    while parent_id:
        parent = conn.execute(
            """
            SELECT id, seq_num, title, task_type, project_id, closed_at, parent_task_id,
                   claimed_by_session_id, labels, validation_criteria
              FROM tasks
             WHERE id = %s
             FOR UPDATE
            """,
            (parent_id,),
        ).fetchone()
        if parent is None or parent["closed_at"] is not None:
            return
        if parent["claimed_by_session_id"] is not None:
            # A claimed ancestor is in-flight work its owner closes through the
            # leaf gates: linked commit, clean attribution, validation, review.
            # Closing it here skips every gate and drops the claim — found-work
            # child #21046 took its worked parent #20969 and epic #20964 that way.
            return
        if has_hold_label(parent) or (
            parent["task_type"] != "epic"
            and normalized_validation_criteria(parent["validation_criteria"]) is not None
        ):
            # A hold label or parent-owned criteria are this parent's own close
            # gates: an open owner decision, a clean window to wait for, a plan
            # still to be written, or acceptance evidence nobody has verified.
            # Auto-closing on the last child (#22421) removed such parents from
            # open lists while that work was unresolved; they close through the
            # gates that check it.
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
