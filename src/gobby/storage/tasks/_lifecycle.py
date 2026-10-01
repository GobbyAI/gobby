"""Task lifecycle operations.

This module provides operations for managing task lifecycle:
- close_task: Close a task
- reopen_task: Reopen a closed/review task
- add_label, remove_label: Manage task labels
- link_commit, unlink_commit: Manage task-commit associations
- delete_task: Delete a task
"""

import json
import logging
import re
from datetime import datetime

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._dispatcher_wake import wake_dispatcher_for_task_change
from gobby.storage.tasks._models import Task, TaskHasChildrenError, TaskHasDependentsError
from gobby.storage.tasks._read import get_task
from gobby.storage.tasks._transitions import close_task as _close_task_transition
from gobby.storage.tasks._transitions import reopen_task as _reopen_task_transition
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)


def repair_closed_candidate(
    db: HubDatabase,
    task_id: str,
    *,
    review_id: str,
    candidate_commit_sha: str,
    expected_closed_commit_sha: str,
    by_session_id: str,
    reason: str,
    preview: bool = True,
) -> dict[str, object]:
    """Correct only the marker of the exact original CLOSED/VALID review, with audit."""
    if len(reason.strip()) < 20 or not re.fullmatch(r"[0-9a-f]{40}", candidate_commit_sha):
        raise ValueError("Repair requires an audit reason and a full reviewed candidate SHA.")
    with db.transaction() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = %s FOR UPDATE", (task_id,)).fetchone()
        review = conn.execute(
            "SELECT * FROM task_close_reviews WHERE id = %s FOR SHARE", (review_id,)
        ).fetchone()
        if task is None or review is None or review["task_id"] != task_id:
            raise ValueError("The original close review must belong to this task.")
        actor = conn.execute(
            "SELECT project_id FROM sessions WHERE id = %s", (by_session_id,)
        ).fetchone()
        if actor is None or actor["project_id"] != task["project_id"]:
            raise ValueError("Repair requires a registered caller in the task project.")
        if (
            task["closed_at"] is None
            or task["validation_status"] != "valid"
            or task["claimed_by_session_id"] is not None
            or task["is_escalated"]
            or task["merge_in_progress"]
        ):
            raise ValueError("Repair requires an unclaimed CLOSED/VALID task outside integration.")
        if task["closed_commit_sha"] != expected_closed_commit_sha:
            raise ValueError("The closed marker changed; inspect canonical state before retrying.")
        if (
            review["status"] != "closed"
            or review["error"] is not None
            or review["completed_at"] is None
            or not review["created_at"] <= task["closed_at"] <= review["completed_at"]
            or task["closed_in_session_id"] != review["caller_session_id"]
        ):
            raise ValueError("The review does not prove this original closed state.")
        arguments = review["close_arguments"]
        payload = review["result_payload"]
        facts = review["stable_facts"]
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        payload = json.loads(payload) if isinstance(payload, str) else payload
        facts = json.loads(facts) if isinstance(facts, str) else facts
        if (
            not isinstance(arguments, dict)
            or not isinstance(payload, dict)
            or not isinstance(facts, dict)
        ):
            raise ValueError("The review's original request and VALID evidence are required.")
        if (
            payload.get("closed") is not True
            or payload.get("validation_status") != "valid"
            or payload.get("status") != "closed"
            or payload.get("event") != "task_close_review_completed"
            or payload.get("review_id") != review_id
            or payload.get("task_id") != task_id
            or arguments.get("commit_sha") != candidate_commit_sha
            or arguments.get("reason") != task["closed_reason"]
        ):
            raise ValueError(
                "Only the exact explicit candidate of a completed VALID review can be repaired."
            )
        links = task["commits"] or []
        links = json.loads(links) if isinstance(links, str) else links
        reviewed_links = facts.get("commit_shas")
        if (
            not isinstance(links, list)
            or not isinstance(reviewed_links, list)
            or not all(isinstance(sha, str) for sha in [*links, *reviewed_links])
            or set(links) != set(reviewed_links)
            or not any(candidate_commit_sha.startswith(sha) for sha in links)
        ):
            raise ValueError("The complete linked commit set must still match the original review.")
        result: dict[str, object] = {
            "task_id": task_id,
            "review_id": review_id,
            "previous_closed_commit_sha": expected_closed_commit_sha,
            "candidate_commit_sha": candidate_commit_sha,
            "closed": True,
            "validation_status": "valid",
            "applied": False,
            "commit_shas": links,
        }
        if preview:
            return result
        conn.execute(
            "UPDATE tasks SET closed_commit_sha = %s, updated_at = %s WHERE id = %s",
            (candidate_commit_sha, utc_now(), task_id),
        )
        # One transaction: an audit failure rolls back the marker correction.
        conn.execute(
            """INSERT INTO task_lifecycle_events
               (task_id, from_state, to_state, reason, by_actor)
               VALUES (%s, 'closed', 'closed', %s, %s)""",
            (
                task_id,
                f"repair_closed_candidate: review={review_id}; "
                f"{expected_closed_commit_sha} -> {candidate_commit_sha}; {reason.strip()}",
                by_session_id,
            ),
        )
        result["applied"] = True
        return result


def close_task(
    db: HubDatabase,
    task_id: str,
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
) -> None:
    """Close a task.

    Args:
        db: Database protocol instance
        task_id: The task ID to close
        reason: Optional reason for closing
        force: If True, close even if there are open children (default: False)
        closed_in_session_id: Session ID where task was closed
        closed_commit_sha: Git commit SHA at time of closing
        validation_override_reason: Why agent bypassed validation (if applicable)

    Raises:
        ValueError: If task not found or has open children (and force=False)
    """
    # Check for open children unless force=True
    if not force:
        open_children = db.fetchall(
            "SELECT id, title FROM tasks WHERE parent_task_id = %s AND closed_at IS NULL",
            (task_id,),
        )
        if open_children:
            child_list = ", ".join(f"{c['id']} ({c['title']})" for c in open_children[:3])
            if len(open_children) > 3:
                child_list += f" and {len(open_children) - 3} more"
            raise ValueError(
                f"Cannot close task {task_id}: has {len(open_children)} open child task(s): {child_list}"
            )

    collected: list[str] = [] if closed_ancestors is None else closed_ancestors
    _close_task_transition(
        db,
        task_id,
        reason=reason,
        force=force,
        closed_in_session_id=closed_in_session_id,
        closed_commit_sha=closed_commit_sha,
        closed_ancestors=collected,
        validation_override_reason=validation_override_reason,
        expected_updated_at=expected_updated_at,
        reset_validation_fail_count=reset_validation_fail_count,
        validation_status=validation_status,
        validation_feedback=validation_feedback,
    )
    for wake_id in (task_id, *collected):
        try:
            wake_dispatcher_for_task_change(db, wake_id)
        except Exception:
            logger.warning(
                "dispatcher_wake_after_task_close_failed",
                extra={"task_id": wake_id},
                exc_info=True,
            )


def reopen_task(
    db: HubDatabase,
    task_id: str,
    reason: str | None = None,
) -> None:
    """Reopen a task to the ready state.

    Works from any non-ready state (in_progress, closed, needs_review, escalated, etc.).
    Clears ownership, closed fields, and resets validation_fail_count.

    Args:
        db: Database protocol instance
        task_id: The task ID to reopen
        reason: Optional reason for reopening

    Raises:
        ValueError: If task not found or already ready
    """
    _reopen_task_transition(db, task_id, reason=reason)


def add_label(db: HubDatabase, task_id: str, label: str) -> Task:
    """Add a label to a task if not present."""
    get_task(db, task_id)  # Validate identity without reading mutation state.
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE tasks
               SET labels = COALESCE(labels, '[]'::jsonb) || jsonb_build_array(%s::text),
                   updated_at = %s
             WHERE id = %s
               AND NOT COALESCE(labels, '[]'::jsonb) @> jsonb_build_array(%s::text)
            """,
            (label, utc_now(), task_id, label),
        )
    return get_task(db, task_id)


def remove_label(db: HubDatabase, task_id: str, label: str) -> Task:
    """Remove a label from a task if present."""
    get_task(db, task_id)  # Validate identity without reading mutation state.
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE tasks
               SET labels = COALESCE(labels, '[]'::jsonb) - %s::text,
                   updated_at = %s
             WHERE id = %s
               AND COALESCE(labels, '[]'::jsonb) @> jsonb_build_array(%s::text)
            """,
            (label, utc_now(), task_id, label),
        )
    return get_task(db, task_id)


def link_commit(db: HubDatabase, task_id: str, commit_sha: str) -> bool:
    """Link a commit SHA to a task.

    Adds the commit SHA to the task's commits array if not already present.
    The caller supplies a Git-resolved canonical short SHA.

    Args:
        db: Database protocol instance
        task_id: The task ID to link the commit to.
        commit_sha: The git commit SHA to link (short or full).

    Returns:
        True if commit was added, False if already present.

    Raises:
        ValueError: If task not found or SHA cannot be resolved.
    """
    if re.fullmatch(r"[0-9a-fA-F]{4,64}", commit_sha) is None:
        raise ValueError(f"Invalid or unresolved commit SHA: {commit_sha}")

    get_task(db, task_id)  # Validate identity without reading mutation state.
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE tasks
               SET commits = COALESCE(commits, '[]'::jsonb)
                             || jsonb_build_array(%s::text),
                   updated_at = %s
             WHERE id = %s
               AND NOT COALESCE(commits, '[]'::jsonb) @> jsonb_build_array(%s::text)
            """,
            (commit_sha, utc_now(), task_id, commit_sha),
        )
    if cursor.rowcount > 0:
        return True
    return False


def unlink_commit(db: HubDatabase, task_id: str, commit_sha: str) -> bool:
    """Unlink a commit SHA from a task.

    Removes the commit SHA from the task's commits array if present.
    Uses the caller-supplied canonical short SHA for exact matching.

    Args:
        db: Database protocol instance
        task_id: The task ID to unlink the commit from.
        commit_sha: The git commit SHA to unlink (short or full).

    Returns:
        True if commit was removed, False if not found.

    Raises:
        ValueError: If task not found.
    """
    get_task(db, task_id)  # Validate identity without reading mutation state.

    if not commit_sha:
        return False

    with db.transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE tasks
               SET commits = NULLIF(commits - %s::text, '[]'::jsonb),
                   updated_at = %s
             WHERE id = %s
               AND COALESCE(commits, '[]'::jsonb) @> jsonb_build_array(%s::text)
            """,
            (commit_sha, utc_now(), task_id, commit_sha),
        )
    if cursor.rowcount > 0:
        return True
    return False


def delete_task(
    db: HubDatabase,
    task_id: str,
    cascade: bool = False,
    unlink: bool = False,
    _visited: set[str] | None = None,
    _origin_path_cache: str | None = None,
) -> bool:
    """Delete a task.

    Args:
        db: Database protocol instance
        task_id: The task ID to delete
        cascade: If True, delete children AND dependent tasks recursively.
                 Dependent cascade follows ``blocks`` edges to tasks that depend
                 on the target — but it does NOT walk into ancestors of the
                 original deletion target (parent / grandparent / root). Pathological
                 wirings where a parent depends on its own children would otherwise
                 cause cascade to climb the parent tree and wipe unrelated subtrees.
        unlink: If True, remove dependency links but preserve dependent tasks
                (ignored if cascade=True)
        _visited: Internal parameter to track visited tasks and prevent infinite recursion
                  when a parent task depends on its children (circular dependency)
        _origin_path_cache: Internal parameter — path_cache of the original deletion
                  target. Captured on the first call and propagated through recursion
                  so the dependent cascade can skip ancestors of the origin.

    Returns:
        True if task was deleted, False if task not found.

    Raises:
        ValueError: If task has children or dependents and neither cascade nor unlink is True.
    """
    # Initialize visited set on first call to prevent infinite recursion
    if _visited is None:
        _visited = set()

    # Skip if already being deleted (prevents cycles when parent depends on children)
    if task_id in _visited:
        return True
    _visited.add(task_id)

    # Check if task exists first; capture path_cache so we can detect ancestors
    # of the original deletion target during dependent cascade.
    existing = db.fetchone("SELECT path_cache FROM tasks WHERE id = %s", (task_id,))
    if not existing:
        return False

    # Capture origin path_cache on the first call. Empty string is fine for
    # legacy rows missing path_cache — the ancestor check below treats those
    # as non-ancestors (no false positives).
    if _origin_path_cache is None:
        _origin_path_cache = existing["path_cache"] or ""

    if not cascade:
        # Check for children
        row = db.fetchone("SELECT 1 FROM tasks WHERE parent_task_id = %s", (task_id,))
        if row:
            raise TaskHasChildrenError(f"Task {task_id} has children. Use cascade=True to delete.")

    if not cascade and not unlink:
        # Check for dependents (tasks that depend on this task)
        dependent_rows = db.fetchall(
            """SELECT t.id, t.seq_num, t.title
               FROM tasks t
               JOIN task_dependencies d ON d.task_id = t.id
               WHERE d.depends_on = %s AND d.dep_type = 'blocks'""",
            (task_id,),
        )
        if dependent_rows:
            refs = [f"#{r['seq_num']}" for r in dependent_rows[:5] if r["seq_num"]]
            refs_str = ", ".join(refs) if refs else str(len(dependent_rows)) + " task(s)"
            if len(dependent_rows) > 5:
                refs_str += f" and {len(dependent_rows) - 5} more"
            raise TaskHasDependentsError(
                f"Task {task_id} has {len(dependent_rows)} dependent task(s): {refs_str}. "
                f"Use cascade=True to delete dependents, or unlink=True to preserve them."
            )

    if cascade:
        # Recursive delete children
        children = db.fetchall("SELECT id FROM tasks WHERE parent_task_id = %s", (task_id,))
        for child in children:
            delete_task(
                db,
                child["id"],
                cascade=True,
                _visited=_visited,
                _origin_path_cache=_origin_path_cache,
            )

        # Delete tasks that depend on this task (only 'blocks' dependencies),
        # excluding ancestors of the original deletion target. path_cache is
        # a dot-separated chain of seq_nums (e.g. "12725.13499.13503"); a task
        # whose path_cache is a prefix of the origin's is an ancestor and
        # following it would wipe unrelated subtrees rooted higher up.
        dependents = db.fetchall(
            """SELECT t.id, t.path_cache FROM tasks t
               JOIN task_dependencies d ON d.task_id = t.id
               WHERE d.depends_on = %s AND d.dep_type = 'blocks'""",
            (task_id,),
        )
        for dep in dependents:
            dep_path = dep["path_cache"] or ""
            if (
                dep_path
                and _origin_path_cache
                and (
                    dep_path == _origin_path_cache or _origin_path_cache.startswith(dep_path + ".")
                )
            ):
                continue
            delete_task(
                db,
                dep["id"],
                cascade=True,
                _visited=_visited,
                _origin_path_cache=_origin_path_cache,
            )

    # Note: if unlink=True, dependency links are removed by ON DELETE CASCADE
    # when the task is deleted - no explicit action needed

    with db.transaction() as conn:
        # Defensive: clear FK references that manual cascade may have missed.
        # tasks.parent_task_id lacks ON DELETE CASCADE, so orphan children
        # would cause an FK constraint failure without this cleanup.
        conn.execute(
            "DELETE FROM task_dependencies WHERE task_id = %s OR depends_on = %s",
            (task_id, task_id),
        )
        conn.execute("UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id = %s", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
    return True
