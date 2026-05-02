"""Core CRUD operations for tasks.

This module provides the core create, read, update operations for tasks.
Functions take a database protocol instance as their first parameter.
"""

import json
import logging
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._blocking import hydrate_task_blocking_state
from gobby.storage.tasks._id import generate_task_id, resolve_task_reference
from gobby.storage.tasks._models import (
    UNSET,
    Isolation,
    Lifecycle,
    MaybeUnset,
    SeqNumCollisionError,
    Task,
    TaskIDCollisionError,
    TaskNotFoundError,
)
from gobby.tasks.state_semantics import (
    lifecycle_stage_from_status,
    normalize_lifecycle_stage,
    project_legacy_status,
)

logger = logging.getLogger(__name__)

_LEGACY_TASK_STATUSES = {
    "open",
    "in_progress",
    "needs_review",
    "review_approved",
    "closed",
    "escalated",
}


def _normalize_skip_stage_labels(skip_stage_labels: Iterable[str]) -> list[str]:
    """Return stable, de-duplicated stage labels to add during build cascade."""
    labels: list[str] = []
    seen: set[str] = set()
    for raw_label in skip_stage_labels:
        label = raw_label.strip()
        if not label or label in seen:
            continue
        labels.append(label)
        seen.add(label)
    return labels


def _skipped_stages(labels: Iterable[str | None] | None) -> set[str]:
    """Extract build stage names from task labels."""
    if not labels:
        return set()
    stages: set[str] = set()
    for label in labels:
        if not isinstance(label, str) or not label.startswith("stage-:"):
            continue
        stage = label.removeprefix("stage-:").strip()
        if stage:
            stages.add(stage)
    return stages


def _decode_labels(labels_json: str | None) -> list[str]:
    """Decode task labels from storage, tolerating legacy nulls."""
    if not labels_json:
        return []
    try:
        parsed: object = json.loads(labels_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


def _is_unattended(task: Any) -> bool:
    """Return whether dispatch should avoid human escalation for a task."""
    return bool(getattr(task, "unattended", False))


def _session_exists(db: DatabaseProtocol, session_id: str) -> bool:
    """Return whether the given session ID exists in storage."""
    return bool(db.fetchone("SELECT 1 FROM sessions WHERE id = ?", (session_id,)))


def _derive_claimed_by_session_id(
    db: DatabaseProtocol,
    *,
    assignee: MaybeUnset[str | None] = UNSET,
    claimed_by_session_id: MaybeUnset[str | None] = UNSET,
) -> MaybeUnset[str | None]:
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


def _normalize_legacy_status(status: Any) -> str:
    """Validate and normalize a projected legacy status."""
    normalized = str(status).strip().lower().replace("-", "_")
    if normalized not in _LEGACY_TASK_STATUSES:
        allowed = ", ".join(sorted(_LEGACY_TASK_STATUSES))
        raise ValueError(f"Invalid task status '{status}'. Expected one of: {allowed}.")
    return normalized


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
    lifecycle_stage: str | None = None,
    labels: list[str] | None = None,
    category: str | None = None,
    validation_criteria: str | None = None,
    assigned_agent: str | None = None,
    additional_skills: list[str] | None = None,
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
    additional_skills_json = (
        json.dumps(additional_skills) if additional_skills is not None else None
    )
    task_id = ""

    # Default validation status
    validation_status = "pending" if validation_criteria else None
    canonical_owner = _derive_claimed_by_session_id(
        db,
        assignee=assignee,
        claimed_by_session_id=claimed_by_session_id,
    )
    canonical_lifecycle_stage = normalize_lifecycle_stage(lifecycle_stage)
    projected_status = project_legacy_status(lifecycle_stage=canonical_lifecycle_stage)
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
                        created_in_session_id, claimed_by_session_id, lifecycle_stage,
                        priority, task_type, assignee,
                        labels, status, created_at, updated_at,
                        validation_status, category,
                        validation_criteria, validation_fail_count,
                        assigned_agent, additional_skills,
                        github_issue_number, github_pr_number, github_repo,
                        linear_issue_id, linear_team_id, seq_num
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        project_id,
                        title,
                        description,
                        parent_task_id,
                        created_in_session_id,
                        canonical_owner,
                        canonical_lifecycle_stage,
                        priority,
                        task_type,
                        assignee,
                        labels_json,
                        projected_status,
                        now,
                        now,
                        validation_status,
                        category,
                        validation_criteria,
                        assigned_agent,
                        additional_skills_json,
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
    task = Task.from_row(row)
    hydrate_task_blocking_state(db, [task])
    return task


def is_blocked_by_deps(task: object) -> bool:
    """Return whether a task has unresolved blocking dependencies."""
    active_blocked_by = getattr(task, "active_blocked_by", None)
    if active_blocked_by is not None:
        return bool(active_blocked_by)
    blocked_by = getattr(task, "blocked_by", None)
    return bool(blocked_by)


def list_automation_candidates(
    db: DatabaseProtocol,
    *,
    project_id: str | None = None,
) -> list[Task]:
    """List unclaimed, unleased, dependency-ready tasks eligible for dispatch."""
    now = datetime.now(UTC).isoformat()
    params: list[Any] = [now]
    project_filter = ""
    if project_id is not None:
        project_filter = "AND tasks.project_id = ?"
        params.append(project_id)

    rows = db.fetchall(
        f"""
        SELECT tasks.*
        FROM tasks
        LEFT JOIN task_dispatch_mutex mutex ON mutex.task_id = tasks.id
        WHERE tasks.allow_automation = 1
          AND tasks.claimed_by_session_id IS NULL
          AND tasks.lifecycle != 'merged'
          AND tasks.closed_at IS NULL
          AND tasks.escalated_at IS NULL
          AND (
              mutex.task_id IS NULL
              OR mutex.lease_until IS NULL
              OR mutex.lease_until < ?
          )
          {project_filter}
        ORDER BY tasks.priority ASC, tasks.seq_num ASC, tasks.created_at ASC
        """,  # nosec B608 - project_filter is static SQL selected above.
        tuple(params),
    )
    tasks = [Task.from_row(row) for row in rows]
    hydrate_task_blocking_state(db, tasks)
    return [task for task in tasks if not is_blocked_by_deps(task)]


def find_task_by_prefix(db: DatabaseProtocol, prefix: str) -> Task | None:
    """Find a task by ID prefix. Returns None if no match or multiple matches."""
    # First try exact match
    row = db.fetchone("SELECT * FROM tasks WHERE id = ?", (prefix,))
    if row:
        task = Task.from_row(row)
        hydrate_task_blocking_state(db, [task])
        return task

    # Try prefix match
    rows = db.fetchall("SELECT * FROM tasks WHERE id LIKE ?", (f"{prefix}%",))
    if len(rows) == 1:
        task = Task.from_row(rows[0])
        hydrate_task_blocking_state(db, [task])
        return task
    return None


def find_tasks_by_prefix(db: DatabaseProtocol, prefix: str) -> list[Task]:
    """Find all tasks matching an ID prefix."""
    rows = db.fetchall("SELECT * FROM tasks WHERE id LIKE ?", (f"{prefix}%",))
    tasks = [Task.from_row(row) for row in rows]
    hydrate_task_blocking_state(db, tasks)
    return tasks


def cascade_build_state_to_subtree(
    db: DatabaseProtocol,
    epic_id: str,
    isolation: Isolation | str,
    unattended: bool | None,
    skip_stage_labels: Iterable[str],
    allow_automation: bool,
    *,
    yolo: bool | None = None,
) -> int:
    """Apply build dispatch state to an epic and every descendant task.

    The cascade intentionally only touches dispatch controls and stage-skip
    labels. Agent assignment, additional skills, and lifecycle fields remain
    task-local decisions.

    Returns the number of tasks updated, including the root epic.
    """
    if unattended is None:
        unattended = bool(yolo)
    normalized_isolation = Isolation(isolation).value
    labels_to_add = _normalize_skip_stage_labels(skip_stage_labels)
    now = datetime.now(UTC).isoformat()

    with db.transaction_immediate() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE subtree(id) AS (
                SELECT id
                FROM tasks
                WHERE id = ?
                UNION ALL
                SELECT child.id
                FROM tasks child
                JOIN subtree parent ON child.parent_task_id = parent.id
            )
            SELECT id, labels
            FROM tasks
            WHERE id IN (SELECT id FROM subtree)
            """,
            (epic_id,),
        ).fetchall()

        if not rows:
            raise ValueError(f"Task {epic_id} not found")

        update_params: list[tuple[str, int, int, str, str, str]] = []
        for row in rows:
            labels = _decode_labels(cast(str | None, row["labels"]))
            known_labels = set(labels)
            for label in labels_to_add:
                if label not in known_labels:
                    labels.append(label)
                    known_labels.add(label)
            update_params.append(
                (
                    json.dumps(labels),
                    int(allow_automation),
                    int(unattended),
                    normalized_isolation,
                    now,
                    cast(str, row["id"]),
                )
            )

        conn.executemany(
            """
            UPDATE tasks
            SET labels = ?,
                allow_automation = ?,
                unattended = ?,
                isolation = ?,
                updated_at = ?
            WHERE id = ?
            """,
            update_params,
        )

    return len(rows)


def update_task(
    db: DatabaseProtocol,
    task_id: str,
    title: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    status: MaybeUnset[str | None] = UNSET,
    priority: MaybeUnset[int | None] = UNSET,
    task_type: MaybeUnset[str | None] = UNSET,
    assignee: MaybeUnset[str | None] = UNSET,
    claimed_by_session_id: MaybeUnset[str | None] = UNSET,
    lifecycle_stage: MaybeUnset[str | None] = UNSET,
    labels: MaybeUnset[list[str] | None] = UNSET,
    parent_task_id: MaybeUnset[str | None] = UNSET,
    closed_reason: MaybeUnset[str | None] = UNSET,
    closed_at: MaybeUnset[str | None] = UNSET,
    closed_in_session_id: MaybeUnset[str | None] = UNSET,
    closed_commit_sha: MaybeUnset[str | None] = UNSET,
    validation_status: MaybeUnset[str | None] = UNSET,
    validation_feedback: MaybeUnset[str | None] = UNSET,
    category: MaybeUnset[str | None] = UNSET,
    validation_criteria: MaybeUnset[str | None] = UNSET,
    validation_fail_count: MaybeUnset[int | None] = UNSET,
    dispatch_failure_count: MaybeUnset[int | None] = UNSET,
    escalated_at: MaybeUnset[str | None] = UNSET,
    escalation_reason: MaybeUnset[str | None] = UNSET,
    github_issue_number: MaybeUnset[int | None] = UNSET,
    github_pr_number: MaybeUnset[int | None] = UNSET,
    github_repo: MaybeUnset[str | None] = UNSET,
    linear_issue_id: MaybeUnset[str | None] = UNSET,
    linear_team_id: MaybeUnset[str | None] = UNSET,
    validation_override_reason: MaybeUnset[str | None] = UNSET,
    lifecycle: MaybeUnset[str | None] = UNSET,
    allow_automation: MaybeUnset[bool | None] = UNSET,
    unattended: MaybeUnset[bool | None] = UNSET,
    yolo: MaybeUnset[bool | None] = UNSET,
    isolation: MaybeUnset[Isolation | str | None] = UNSET,
    assigned_agent: MaybeUnset[str | None] = UNSET,
    additional_skills: MaybeUnset[list[str] | None] = UNSET,
) -> bool:
    """Internal storage primitive for task field updates.

    This function intentionally remains permissive because lifecycle and
    reconciliation helpers need an atomic write primitive below the manager
    policy boundary. External callers should use LocalTaskManager methods
    instead of calling this directly.

    Returns True if parent_task_id was changed (indicating path cache needs update).
    """
    current_task = get_task(db, task_id)
    updates: list[str] = []
    params: list[Any] = []
    now = datetime.now(UTC).isoformat()
    task_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(tasks)")}

    if title is not UNSET:
        updates.append("title = ?")
        params.append(title)
    if description is not UNSET:
        updates.append("description = ?")
        params.append(description)
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
    if validation_status is not UNSET:
        updates.append("validation_status = ?")
        params.append(validation_status)
    if validation_feedback is not UNSET:
        updates.append("validation_feedback = ?")
        params.append(validation_feedback)
    if category is not UNSET:
        updates.append("category = ?")
        params.append(category)
    if validation_criteria is not UNSET:
        updates.append("validation_criteria = ?")
        params.append(validation_criteria)
    if validation_fail_count is not UNSET:
        updates.append("validation_fail_count = ?")
        params.append(validation_fail_count)
    if dispatch_failure_count is not UNSET:
        updates.append("dispatch_failure_count = ?")
        params.append(dispatch_failure_count)
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
    if validation_override_reason is not UNSET:
        updates.append("validation_override_reason = ?")
        params.append(validation_override_reason)
    if lifecycle is not UNSET:
        if lifecycle is None:
            raise ValueError("lifecycle cannot be None")
        updates.append("lifecycle = ?")
        params.append(Lifecycle(cast(str, lifecycle)).value)
    if allow_automation is not UNSET:
        updates.append("allow_automation = ?")
        params.append(int(bool(allow_automation)))
    if unattended is UNSET and yolo is not UNSET:
        unattended = yolo
    if unattended is not UNSET:
        updates.append("unattended = ?")
        params.append(int(bool(unattended)))
    if isolation is not UNSET:
        if isolation is None:
            raise ValueError("isolation cannot be None")
        updates.append("isolation = ?")
        params.append(Isolation(cast(str, isolation)).value)
    if assigned_agent is not UNSET:
        updates.append("assigned_agent = ?")
        params.append(assigned_agent)
    if additional_skills is not UNSET:
        updates.append("additional_skills = ?")
        params.append(json.dumps(additional_skills) if additional_skills is not None else None)
    normalized_status = _normalize_legacy_status(status) if status is not UNSET else None
    next_lifecycle_stage = current_task.lifecycle_stage
    if lifecycle_stage is not UNSET:
        next_lifecycle_stage = normalize_lifecycle_stage(cast(str | None, lifecycle_stage))
    if normalized_status in {"open", "in_progress", "needs_review", "review_approved"}:
        next_lifecycle_stage = lifecycle_stage_from_status(normalized_status)

    next_closed_at = current_task.closed_at if closed_at is UNSET else cast(str | None, closed_at)
    next_escalated_at = (
        current_task.escalated_at if escalated_at is UNSET else cast(str | None, escalated_at)
    )

    if normalized_status == "closed" and next_closed_at is None:
        next_closed_at = now
    elif normalized_status and normalized_status != "closed" and closed_at is UNSET:
        next_closed_at = None

    if normalized_status == "escalated" and next_escalated_at is None:
        next_escalated_at = now
    elif normalized_status and normalized_status != "escalated" and escalated_at is UNSET:
        next_escalated_at = None

    state_inputs_touched = any(
        value is not UNSET for value in (status, lifecycle_stage, closed_at, escalated_at)
    )
    if state_inputs_touched:
        updates.append("lifecycle_stage = ?")
        params.append(next_lifecycle_stage)
        updates.append("closed_at = ?")
        params.append(next_closed_at)
        updates.append("escalated_at = ?")
        params.append(next_escalated_at)
        updates.append("status = ?")
        params.append(
            project_legacy_status(
                lifecycle_stage=next_lifecycle_stage,
                closed_at=next_closed_at,
                escalated_at=next_escalated_at,
            )
        )
        if "is_escalated" in task_columns:
            updates.append("is_escalated = ?")
            params.append(1 if next_escalated_at else 0)

    if closed_reason is not UNSET:
        updates.append("closed_reason = ?")
        params.append(closed_reason)
    elif current_task.closed_at and next_closed_at is None:
        updates.append("closed_reason = ?")
        params.append(None)
    if closed_in_session_id is not UNSET:
        updates.append("closed_in_session_id = ?")
        params.append(closed_in_session_id)
    elif current_task.closed_at and next_closed_at is None:
        updates.append("closed_in_session_id = ?")
        params.append(None)
    if closed_commit_sha is not UNSET:
        updates.append("closed_commit_sha = ?")
        params.append(closed_commit_sha)
    elif current_task.closed_at and next_closed_at is None:
        updates.append("closed_commit_sha = ?")
        params.append(None)

    if escalation_reason is not UNSET:
        updates.append("escalation_reason = ?")
        params.append(escalation_reason)
    elif current_task.escalated_at and next_escalated_at is None:
        updates.append("escalation_reason = ?")
        params.append(None)

    if current_task.closed_at and next_closed_at is None:
        if validation_status is UNSET:
            updates.append("validation_status = ?")
            params.append(None)
        if validation_feedback is UNSET:
            updates.append("validation_feedback = ?")
            params.append(None)

    if not updates:
        return False

    updates.append("updated_at = ?")
    params.append(now)

    params.append(task_id)  # for WHERE clause

    sql = f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?"  # nosec B608

    with db.transaction() as conn:
        cursor = conn.execute(sql, tuple(params))
        if cursor.rowcount == 0:
            raise ValueError(f"Task {task_id} not found")

    # Return whether parent_task_id was changed (caller should update path cache)
    return parent_task_id is not UNSET
