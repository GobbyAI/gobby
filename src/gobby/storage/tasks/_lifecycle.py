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
from datetime import UTC, datetime
from pathlib import Path

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._crud import get_task, update_task
from gobby.storage.tasks._models import Task

logger = logging.getLogger(__name__)


def close_task(
    db: DatabaseProtocol,
    task_id: str,
    reason: str | None = None,
    force: bool = False,
    closed_in_session_id: str | None = None,
    closed_commit_sha: str | None = None,
    validation_override_reason: str | None = None,
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
            "SELECT id, title FROM tasks WHERE parent_task_id = ? AND status != 'closed'",
            (task_id,),
        )
        if open_children:
            child_list = ", ".join(f"{c['id']} ({c['title']})" for c in open_children[:3])
            if len(open_children) > 3:
                child_list += f" and {len(open_children) - 3} more"
            raise ValueError(
                f"Cannot close task {task_id}: has {len(open_children)} open child task(s): {child_list}"
            )

    # Check if task is being closed from needs_review state (user acceptance)
    current_task = get_task(db, task_id)
    accepted_by_user = current_task.status == "needs_review" if current_task else False

    now = datetime.now(UTC).isoformat()
    with db.transaction() as conn:
        cursor = conn.execute(
            """UPDATE tasks SET
                status = 'closed',
                closed_reason = ?,
                closed_at = ?,
                closed_in_session_id = ?,
                closed_commit_sha = ?,
                validation_override_reason = ?,
                accepted_by_user = ?,
                updated_at = ?
            WHERE id = ?""",
            (
                reason,
                now,
                closed_in_session_id,
                closed_commit_sha,
                validation_override_reason,
                1 if accepted_by_user else 0,
                now,
                task_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Task {task_id} not found")

    # Update any associated worktrees to merged status (outside transaction)
    # This is best-effort and should not roll back the task close
    try:
        db.execute(
            """UPDATE worktrees SET status = 'merged', updated_at = ?
            WHERE task_id = ? AND status = 'active'""",
            (now, task_id),
        )
    except Exception as wt_err:
        # Worktree update is best-effort, don't fail task close
        logger.debug(f"Failed to update worktree status for task {task_id}: {wt_err}")


def reopen_task(
    db: DatabaseProtocol,
    task_id: str,
    reason: str | None = None,
) -> None:
    """Reopen a task to open status.

    Works on any non-open status (in_progress, closed, needs_review, escalated, etc.).
    Clears closed fields, assignee, and resets validation_fail_count.

    Args:
        db: Database protocol instance
        task_id: The task ID to reopen
        reason: Optional reason for reopening

    Raises:
        ValueError: If task not found or already open
    """
    task = get_task(db, task_id)
    if task.status == "open":
        raise ValueError(f"Task {task_id} is already open")

    now = datetime.now(UTC).isoformat()

    # Build description update if reason provided
    new_description = task.description or ""
    if reason:
        reopen_note = f"\n\n[Reopened: {reason}]"
        new_description = new_description + reopen_note

    with db.transaction() as conn:
        conn.execute(
            """UPDATE tasks SET
                status = 'open',
                assignee = NULL,
                closed_reason = NULL,
                closed_at = NULL,
                closed_in_session_id = NULL,
                closed_commit_sha = NULL,
                accepted_by_user = 0,
                validation_fail_count = 0,
                description = ?,
                updated_at = ?
            WHERE id = ?""",
            (new_description if reason else task.description, now, task_id),
        )

    # Reactivate any merged or abandoned worktrees for this task (outside transaction)
    # This is best-effort and should not roll back the task reopen
    try:
        db.execute(
            """UPDATE worktrees SET status = 'active', updated_at = ?
            WHERE task_id = ? AND status IN ('merged', 'abandoned')""",
            (now, task_id),
        )
    except Exception as wt_err:
        # Worktree update is best-effort, don't fail task reopen
        logger.debug(f"Failed to reactivate worktree for task {task_id}: {wt_err}")


def add_label(db: DatabaseProtocol, task_id: str, label: str) -> Task:
    """Add a label to a task if not present."""
    task = get_task(db, task_id)
    labels = task.labels or []
    if label not in labels:
        labels.append(label)
        update_task(db, task_id, labels=labels)
        return get_task(db, task_id)
    return task


def remove_label(db: DatabaseProtocol, task_id: str, label: str) -> Task:
    """Remove a label from a task if present."""
    task = get_task(db, task_id)
    labels = task.labels or []
    if label in labels:
        labels.remove(label)
        update_task(db, task_id, labels=labels)
        return get_task(db, task_id)
    return task


def link_commit(
    db: DatabaseProtocol, task_id: str, commit_sha: str, cwd: str | Path | None = None
) -> bool:
    """Link a commit SHA to a task.

    Adds the commit SHA to the task's commits array if not already present.
    The SHA is normalized to dynamic short format for consistency.

    Args:
        db: Database protocol instance
        task_id: The task ID to link the commit to.
        commit_sha: The git commit SHA to link (short or full).
        cwd: Working directory for git operations (defaults to current directory).

    Returns:
        True if commit was added, False if already present.

    Raises:
        ValueError: If task not found or SHA cannot be resolved.
    """
    from gobby.utils.git import normalize_commit_sha

    # Normalize SHA to dynamic short format
    normalized_sha = normalize_commit_sha(commit_sha, cwd=cwd)
    if not normalized_sha:
        raise ValueError(f"Invalid or unresolved commit SHA: {commit_sha}")

    task = get_task(db, task_id)  # Raises if not found
    commits = task.commits or []
    if normalized_sha not in commits:
        commits.append(normalized_sha)
        # Update the commits column in the database
        now = datetime.now(UTC).isoformat()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET commits = ?, updated_at = ? WHERE id = ?",
                (json.dumps(commits), now, task_id),
            )
        return True
    return False


def unlink_commit(
    db: DatabaseProtocol, task_id: str, commit_sha: str, cwd: str | Path | None = None
) -> bool:
    """Unlink a commit SHA from a task.

    Removes the commit SHA from the task's commits array if present.
    Uses normalized SHA for exact matching.

    Args:
        db: Database protocol instance
        task_id: The task ID to unlink the commit from.
        commit_sha: The git commit SHA to unlink (short or full).
        cwd: Working directory for git operations (defaults to current directory).

    Returns:
        True if commit was removed, False if not found.

    Raises:
        ValueError: If task not found.
    """
    from gobby.utils.git import normalize_commit_sha

    # Normalize SHA to dynamic short format
    normalized_sha = normalize_commit_sha(commit_sha, cwd=cwd)

    task = get_task(db, task_id)  # Raises if not found
    commits = task.commits or []

    # Find matching commit by exact normalized SHA
    sha_to_remove = None
    if normalized_sha:
        for stored_sha in commits:
            if stored_sha == normalized_sha:
                sha_to_remove = stored_sha
                break

    if sha_to_remove:
        commits.remove(sha_to_remove)
        # Update the commits column in the database
        now = datetime.now(UTC).isoformat()
        commits_json = json.dumps(commits) if commits else None
        with db.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET commits = ?, updated_at = ? WHERE id = ?",
                (commits_json, now, task_id),
            )
        return True
    return False


def delete_task(
    db: DatabaseProtocol,
    task_id: str,
    cascade: bool = False,
    unlink: bool = False,
    _visited: set[str] | None = None,
) -> bool:
    """Delete a task.

    Args:
        db: Database protocol instance
        task_id: The task ID to delete
        cascade: If True, delete children AND dependent tasks recursively
        unlink: If True, remove dependency links but preserve dependent tasks
                (ignored if cascade=True)
        _visited: Internal parameter to track visited tasks and prevent infinite recursion
                  when a parent task depends on its children (circular dependency)

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

    # Check if task exists first
    existing = db.fetchone("SELECT 1 FROM tasks WHERE id = ?", (task_id,))
    if not existing:
        return False

    if not cascade:
        # Check for children
        row = db.fetchone("SELECT 1 FROM tasks WHERE parent_task_id = ?", (task_id,))
        if row:
            raise ValueError(f"Task {task_id} has children. Use cascade=True to delete.")

    if not cascade and not unlink:
        # Check for dependents (tasks that depend on this task)
        dependent_rows = db.fetchall(
            """SELECT t.id, t.seq_num, t.title
               FROM tasks t
               JOIN task_dependencies d ON d.task_id = t.id
               WHERE d.depends_on = ? AND d.dep_type = 'blocks'""",
            (task_id,),
        )
        if dependent_rows:
            refs = [f"#{r['seq_num']}" for r in dependent_rows[:5] if r["seq_num"]]
            refs_str = ", ".join(refs) if refs else str(len(dependent_rows)) + " task(s)"
            if len(dependent_rows) > 5:
                refs_str += f" and {len(dependent_rows) - 5} more"
            raise ValueError(
                f"Task {task_id} has {len(dependent_rows)} dependent task(s): {refs_str}. "
                f"Use cascade=True to delete dependents, or unlink=True to preserve them."
            )

    if cascade:
        # Recursive delete children
        children = db.fetchall("SELECT id FROM tasks WHERE parent_task_id = ?", (task_id,))
        for child in children:
            delete_task(db, child["id"], cascade=True, _visited=_visited)

        # Delete tasks that depend on this task (only 'blocks' dependencies)
        dependents = db.fetchall(
            """SELECT t.id FROM tasks t
               JOIN task_dependencies d ON d.task_id = t.id
               WHERE d.depends_on = ? AND d.dep_type = 'blocks'""",
            (task_id,),
        )
        for dep in dependents:
            delete_task(db, dep["id"], cascade=True, _visited=_visited)

    # Note: if unlink=True, dependency links are removed by ON DELETE CASCADE
    # when the task is deleted - no explicit action needed

    with db.transaction() as conn:
        # Defensive: clear FK references that manual cascade may have missed.
        # tasks.parent_task_id lacks ON DELETE CASCADE, so orphan children
        # would cause an FK constraint failure without this cleanup.
        conn.execute(
            "DELETE FROM task_dependencies WHERE task_id = ? OR depends_on = ?", (task_id, task_id)
        )
        conn.execute("UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id = ?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return True
