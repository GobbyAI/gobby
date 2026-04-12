"""Core CRUD operations for tasks.

This module provides the core create, read, update operations for tasks.
Functions take a database protocol instance as their first parameter.
"""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._id import generate_task_id, resolve_task_reference
from gobby.storage.tasks._models import (
    UNSET,
    SeqNumCollisionError,
    Task,
    TaskIDCollisionError,
    TaskNotFoundError,
)

logger = logging.getLogger(__name__)


def _session_exists(db: DatabaseProtocol, session_id: str) -> bool:
    """Return whether the given session ID exists in storage."""
    return bool(db.fetchone("SELECT 1 FROM sessions WHERE id = ?", (session_id,)))


def _derive_claimed_by_session_id(
    db: DatabaseProtocol,
    *,
    assignee: Any = UNSET,
    claimed_by_session_id: Any = UNSET,
) -> Any:
    """Project canonical ownership from explicit owner or session assignee.

    `claimed_by_session_id` is authoritative when explicitly provided.
    When only `assignee` is supplied, we mirror it into canonical ownership
    only if it resolves to a real session ID. This preserves compatibility-only
    assignee values such as web-chat conversation IDs.
    """
    if claimed_by_session_id is not UNSET:
        return claimed_by_session_id
    if assignee is UNSET:
        return UNSET
    if assignee is None:
        return None
    if isinstance(assignee, str) and _session_exists(db, assignee):
        return assignee
    return UNSET


def create_task(
    db: DatabaseProtocol,
    project_id: str,
    title: str,
    description: str | None = None,
    parent_task_id: str | None = None,
    created_in_session_id: str | None = None,
    priority: int = 2,
    task_type: str = "task",
    assignee: str | None = None,
    claimed_by_session_id: str | None = None,
    labels: list[str] | None = None,
    category: str | None = None,
    expansion_context: str | None = None,
    validation_criteria: str | None = None,
    github_issue_number: int | None = None,
    github_pr_number: int | None = None,
    github_repo: str | None = None,
    linear_issue_id: str | None = None,
    linear_team_id: str | None = None,
) -> str:
    """Create a new task with collision handling.

    Returns the task_id of the created task.
    """
    max_retries = 3
    now = datetime.now(UTC).isoformat()

    # Serialize labels
    labels_json = json.dumps(labels) if labels else None
    task_id = ""

    # Default validation status
    validation_status = "pending" if validation_criteria else None
    canonical_owner = _derive_claimed_by_session_id(
        db,
        assignee=assignee,
        claimed_by_session_id=claimed_by_session_id,
    )
    if canonical_owner is UNSET:
        canonical_owner = None

    for attempt in range(max_retries + 1):
        try:
            task_id = generate_task_id(project_id, salt=str(attempt))

            with db.transaction_immediate() as conn:
                # Get next seq_num for this project (auto-increment per project)
                max_seq_row = conn.execute(
                    "SELECT MAX(seq_num) as max_seq FROM tasks WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                next_seq_num = ((max_seq_row["max_seq"] if max_seq_row else None) or 0) + 1

                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, project_id, title, description, parent_task_id,
                        created_in_session_id, claimed_by_session_id, priority, task_type, assignee,
                        labels, status, created_at, updated_at,
                        validation_status, category, expansion_context,
                        validation_criteria, validation_fail_count,
                        github_issue_number, github_pr_number, github_repo,
                        linear_issue_id, linear_team_id, seq_num
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        project_id,
                        title,
                        description,
                        parent_task_id,
                        created_in_session_id,
                        canonical_owner,
                        priority,
                        task_type,
                        assignee,
                        labels_json,
                        now,
                        now,
                        validation_status,
                        category,
                        expansion_context,
                        validation_criteria,
                        github_issue_number,
                        github_pr_number,
                        github_repo,
                        linear_issue_id,
                        linear_team_id,
                        next_seq_num,
                    ),
                )

                logger.debug(f"Created task {task_id} in project {project_id}")

                # Compute and store path_cache for the new task
                # Build path by traversing parent chain
                path_parts: list[str] = [str(next_seq_num)]
                current_parent = parent_task_id
                max_depth = 100
                depth = 0
                while current_parent and depth < max_depth:
                    parent_row = conn.execute(
                        "SELECT seq_num, parent_task_id FROM tasks WHERE id = ?",
                        (current_parent,),
                    ).fetchone()
                    if not parent_row or parent_row["seq_num"] is None:
                        break
                    path_parts.append(str(parent_row["seq_num"]))
                    current_parent = parent_row["parent_task_id"]
                    depth += 1

                path_parts.reverse()
                path_cache = ".".join(path_parts)
                conn.execute(
                    "UPDATE tasks SET path_cache = ? WHERE id = ?",
                    (path_cache, task_id),
                )

            return task_id

        except sqlite3.IntegrityError as e:
            error_msg = str(e)
            # Check if it's a primary key violation (ID collision)
            if "UNIQUE constraint failed: tasks.id" in error_msg or "tasks.id" in error_msg:
                if attempt == max_retries:
                    raise TaskIDCollisionError(
                        f"Failed to generate unique task ID after {max_retries} retries"
                    ) from e
                logger.warning(f"Task ID collision for {task_id}, retrying...")
                continue
            # Check if it's a seq_num collision (concurrent insert race)
            if "idx_tasks_seq_num" in error_msg or "tasks.seq_num" in error_msg:
                if attempt == max_retries:
                    raise SeqNumCollisionError(
                        f"Failed to allocate unique seq_num after {max_retries} retries"
                    ) from e
                logger.warning(f"Task seq_num collision for project {project_id}, retrying...")
                continue
            raise

    raise TaskIDCollisionError("Unreachable")


def get_task(db: DatabaseProtocol, task_id: str, project_id: str | None = None) -> Task:
    """Get a task by ID or reference.

    Accepts multiple formats:
      - UUID: Direct lookup
      - #N: Project-scoped seq_num (requires project_id)
      - N: Plain seq_num (requires project_id)

    Args:
        db: Database protocol instance
        task_id: Task identifier in any supported format
        project_id: Required for #N and N formats

    Returns:
        The Task object

    Raises:
        ValueError: If task not found or format requires project_id
    """
    # Check if this looks like a seq_num reference (#N or plain N)
    is_seq_ref = task_id.startswith("#") or task_id.isdigit()

    if is_seq_ref:
        if not project_id:
            raise ValueError(f"Task {task_id} requires project_id for seq_num lookup")
        try:
            resolved_id = resolve_task_reference(db, task_id, project_id)
            task_id = resolved_id
        except TaskNotFoundError as e:
            raise ValueError(str(e)) from e

    row = db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not row:
        raise ValueError(f"Task {task_id} not found")
    return Task.from_row(row)


def find_task_by_prefix(db: DatabaseProtocol, prefix: str) -> Task | None:
    """Find a task by ID prefix. Returns None if no match or multiple matches."""
    # First try exact match
    row = db.fetchone("SELECT * FROM tasks WHERE id = ?", (prefix,))
    if row:
        return Task.from_row(row)

    # Try prefix match
    rows = db.fetchall("SELECT * FROM tasks WHERE id LIKE ?", (f"{prefix}%",))
    if len(rows) == 1:
        return Task.from_row(rows[0])
    return None


def find_tasks_by_prefix(db: DatabaseProtocol, prefix: str) -> list[Task]:
    """Find all tasks matching an ID prefix."""
    rows = db.fetchall("SELECT * FROM tasks WHERE id LIKE ?", (f"{prefix}%",))
    return [Task.from_row(row) for row in rows]


def update_task(
    db: DatabaseProtocol,
    task_id: str,
    title: Any = UNSET,
    description: Any = UNSET,
    status: Any = UNSET,
    priority: Any = UNSET,
    task_type: Any = UNSET,
    assignee: Any = UNSET,
    claimed_by_session_id: Any = UNSET,
    labels: Any = UNSET,
    parent_task_id: Any = UNSET,
    closed_reason: Any = UNSET,
    closed_at: Any = UNSET,
    closed_in_session_id: Any = UNSET,
    closed_commit_sha: Any = UNSET,
    validation_status: Any = UNSET,
    validation_feedback: Any = UNSET,
    category: Any = UNSET,
    expansion_context: Any = UNSET,
    validation_criteria: Any = UNSET,
    validation_fail_count: Any = UNSET,
    dispatch_failure_count: Any = UNSET,
    escalated_at: Any = UNSET,
    escalation_reason: Any = UNSET,
    github_issue_number: Any = UNSET,
    github_pr_number: Any = UNSET,
    github_repo: Any = UNSET,
    linear_issue_id: Any = UNSET,
    linear_team_id: Any = UNSET,
    expansion_status: Any = UNSET,
    validation_override_reason: Any = UNSET,
) -> bool:
    """Update task fields.

    Returns True if parent_task_id was changed (indicating path cache needs update).
    """
    updates: list[str] = []
    params: list[Any] = []

    if title is not UNSET:
        updates.append("title = ?")
        params.append(title)
    if description is not UNSET:
        updates.append("description = ?")
        params.append(description)
    if status is not UNSET:
        updates.append("status = ?")
        params.append(status)
    if priority is not UNSET:
        updates.append("priority = ?")
        params.append(priority)
    if task_type is not UNSET:
        updates.append("task_type = ?")
        params.append(task_type)
    if assignee is not UNSET:
        updates.append("assignee = ?")
        params.append(assignee)
    derived_claimed_by_session_id = _derive_claimed_by_session_id(
        db,
        assignee=assignee,
        claimed_by_session_id=claimed_by_session_id,
    )
    if derived_claimed_by_session_id is not UNSET:
        updates.append("claimed_by_session_id = ?")
        params.append(derived_claimed_by_session_id)
    if labels is not UNSET:
        updates.append("labels = ?")
        if labels is None:
            params.append("[]")
        else:
            params.append(json.dumps(labels))
    if parent_task_id is not UNSET:
        updates.append("parent_task_id = ?")
        params.append(parent_task_id)
    if closed_reason is not UNSET:
        updates.append("closed_reason = ?")
        params.append(closed_reason)
    if closed_at is not UNSET:
        updates.append("closed_at = ?")
        params.append(closed_at)
    if closed_in_session_id is not UNSET:
        updates.append("closed_in_session_id = ?")
        params.append(closed_in_session_id)
    if closed_commit_sha is not UNSET:
        updates.append("closed_commit_sha = ?")
        params.append(closed_commit_sha)
    if validation_status is not UNSET:
        updates.append("validation_status = ?")
        params.append(validation_status)
    if validation_feedback is not UNSET:
        updates.append("validation_feedback = ?")
        params.append(validation_feedback)
    if category is not UNSET:
        updates.append("category = ?")
        params.append(category)
    if expansion_context is not UNSET:
        updates.append("expansion_context = ?")
        params.append(expansion_context)
    if validation_criteria is not UNSET:
        updates.append("validation_criteria = ?")
        params.append(validation_criteria)
    if validation_fail_count is not UNSET:
        updates.append("validation_fail_count = ?")
        params.append(validation_fail_count)
    if dispatch_failure_count is not UNSET:
        updates.append("dispatch_failure_count = ?")
        params.append(dispatch_failure_count)
    if escalated_at is not UNSET:
        updates.append("escalated_at = ?")
        params.append(escalated_at)
    if escalation_reason is not UNSET:
        updates.append("escalation_reason = ?")
        params.append(escalation_reason)
    if github_issue_number is not UNSET:
        updates.append("github_issue_number = ?")
        params.append(github_issue_number)
    if github_pr_number is not UNSET:
        updates.append("github_pr_number = ?")
        params.append(github_pr_number)
    if github_repo is not UNSET:
        updates.append("github_repo = ?")
        params.append(github_repo)
    if linear_issue_id is not UNSET:
        updates.append("linear_issue_id = ?")
        params.append(linear_issue_id)
    if linear_team_id is not UNSET:
        updates.append("linear_team_id = ?")
        params.append(linear_team_id)
    if expansion_status is not UNSET:
        updates.append("expansion_status = ?")
        params.append(expansion_status)
    if validation_override_reason is not UNSET:
        updates.append("validation_override_reason = ?")
        params.append(validation_override_reason)
    # Auto-reset closed metadata when transitioning from 'closed' to any other status
    if status is not UNSET and status != "closed":
        current_task = get_task(db, task_id)
        if current_task and current_task.status == "closed":
            # Wipe closed metadata
            updates.append("closed_reason = ?")
            params.append(None)
            updates.append("closed_at = ?")
            params.append(None)
            updates.append("closed_in_session_id = ?")
            params.append(None)
            updates.append("closed_commit_sha = ?")
            params.append(None)

            # Wipe validation metadata if not explicitly set
            if validation_status is UNSET:
                updates.append("validation_status = ?")
                params.append(None)
            if validation_feedback is UNSET:
                updates.append("validation_feedback = ?")
                params.append(None)

    if not updates:
        return False

    updates.append("updated_at = ?")
    params.append(datetime.now(UTC).isoformat())

    params.append(task_id)  # for WHERE clause

    sql = f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?"  # nosec B608

    with db.transaction() as conn:
        cursor = conn.execute(sql, tuple(params))
        if cursor.rowcount == 0:
            raise ValueError(f"Task {task_id} not found")

    # Return whether parent_task_id was changed (caller should update path cache)
    return parent_task_id is not UNSET
