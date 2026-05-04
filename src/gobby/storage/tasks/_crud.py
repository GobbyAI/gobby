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
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.storage.tasks._models import (
    UNSET,
    Isolation,
    MaybeUnset,
    SeqNumCollisionError,
    Task,
    TaskIDCollisionError,
    TaskNotFoundError,
    validate_task_type,
)
from gobby.storage.tasks._stage_hydration import hydrate_task_stage_state
from gobby.storage.tasks._stage_states import StageStatesManager
from gobby.storage.tasks._stage_types import StageManifestSpec

logger = logging.getLogger(__name__)


def _manifest_specs_for_task_type(
    stage_states: StageStatesManager,
    task_type: str,
    skip_stages: set[str],
) -> list[StageManifestSpec]:
    defaults = stage_states.registry.list_default_stages(task_type)
    if not defaults and task_type != "task":
        defaults = stage_states.registry.list_default_stages("task")
    manifest = [stage_name for stage_name, _position in defaults if stage_name not in skip_stages]
    return [
        StageManifestSpec(stage_name=stage_name, position=index)
        for index, stage_name in enumerate(manifest)
    ]


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
    task_type = validate_task_type(task_type)
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
                        created_in_session_id, claimed_by_session_id,
                        priority, task_type, assignee,
                        labels, created_at, updated_at,
                        validation_status, category,
                        validation_criteria, validation_fail_count,
                        assigned_agent, additional_skills,
                        github_issue_number, github_pr_number, github_repo,
                        linear_issue_id, linear_team_id, seq_num
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
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
    hydrate_task_stage_state(db, [task])
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
        JOIN task_stage_states current_stage
          ON current_stage.task_id = tasks.id
         AND current_stage.state != 'done'
         AND current_stage.position = (
             SELECT MIN(stage_scan.position)
               FROM task_stage_states stage_scan
              WHERE stage_scan.task_id = tasks.id
                AND stage_scan.state != 'done'
         )
        LEFT JOIN task_dispatch_mutex mutex ON mutex.task_id = tasks.id
        WHERE tasks.allow_automation = 1
          AND tasks.claimed_by_session_id IS NULL
          AND tasks.closed_at IS NULL
          AND tasks.escalated_at IS NULL
          AND COALESCE(tasks.is_escalated, 0) = 0
          AND current_stage.state IN ('ready', 'in_progress', 'needs_review', 'review_approved')
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
    hydrate_task_stage_state(db, tasks)
    hydrate_task_blocking_state(db, tasks)
    return [task for task in tasks if not is_blocked_by_deps(task)]


def find_task_by_prefix(db: DatabaseProtocol, prefix: str) -> Task | None:
    """Find a task by ID prefix. Returns None if no match or multiple matches."""
    # First try exact match
    row = db.fetchone("SELECT * FROM tasks WHERE id = ?", (prefix,))
    if row:
        task = Task.from_row(row)
        hydrate_task_stage_state(db, [task])
        hydrate_task_blocking_state(db, [task])
        return task

    # Try prefix match
    rows = db.fetchall("SELECT * FROM tasks WHERE id LIKE ?", (f"{prefix}%",))
    if len(rows) == 1:
        task = Task.from_row(rows[0])
        hydrate_task_stage_state(db, [task])
        hydrate_task_blocking_state(db, [task])
        return task
    return None


def find_tasks_by_prefix(db: DatabaseProtocol, prefix: str) -> list[Task]:
    """Find all tasks matching an ID prefix."""
    rows = db.fetchall("SELECT * FROM tasks WHERE id LIKE ?", (f"{prefix}%",))
    tasks = [Task.from_row(row) for row in rows]
    hydrate_task_stage_state(db, tasks)
    hydrate_task_blocking_state(db, tasks)
    return tasks


def cascade_build_state_to_subtree(
    db: DatabaseProtocol,
    epic_id: str,
    isolation: Isolation | str,
    unattended: bool | None,
    allow_automation: bool,
    *,
    skip_stages: Iterable[str] = (),
    yolo: bool | None = None,
) -> int:
    """Apply build dispatch state to an epic and every descendant task.

    The cascade intentionally only touches dispatch controls and child manifest
    shape. Agent assignment, additional skills, and lifecycle fields remain
    task-local decisions.

    Returns the number of tasks updated, including the root epic.
    """
    if unattended is None:
        unattended = bool(yolo)
    normalized_isolation = Isolation(isolation).value
    skipped = {stage.strip() for stage in skip_stages if stage.strip()}
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
            SELECT id, task_type
            FROM tasks
            WHERE id IN (SELECT id FROM subtree)
            """,
            (epic_id,),
        ).fetchall()

        if not rows:
            raise ValueError(f"Task {epic_id} not found")

        update_params: list[tuple[int, int, str, str, str]] = []
        for row in rows:
            update_params.append(
                (
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
            SET allow_automation = ?,
                unattended = ?,
                isolation = ?,
                updated_at = ?
            WHERE id = ?
            """,
            update_params,
        )

    stage_states = StageStatesManager(db, TaskLifecycleEventManager(db))
    for row in rows:
        task_id = cast(str, row["id"])
        if task_id == epic_id:
            continue
        specs = _manifest_specs_for_task_type(stage_states, cast(str, row["task_type"]), skipped)
        if specs:
            stage_states.initialize_manifest(task_id, specs, by_session_id=None)

    return len(rows)


def update_task(
    db: DatabaseProtocol,
    task_id: str,
    title: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    priority: MaybeUnset[int | None] = UNSET,
    task_type: MaybeUnset[str | None] = UNSET,
    assignee: MaybeUnset[str | None] = UNSET,
    claimed_by_session_id: MaybeUnset[str | None] = UNSET,
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
        params.append(validate_task_type(cast(str | None, task_type)))
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
    next_closed_at = current_task.closed_at if closed_at is UNSET else cast(str | None, closed_at)
    next_escalated_at = (
        current_task.escalated_at if escalated_at is UNSET else cast(str | None, escalated_at)
    )

    if closed_at is not UNSET:
        updates.append("closed_at = ?")
        params.append(next_closed_at)
    if escalated_at is not UNSET:
        updates.append("escalated_at = ?")
        params.append(next_escalated_at)
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


def update_task_metadata(
    db: DatabaseProtocol,
    task_id: str,
    *,
    title: MaybeUnset[str | None] = UNSET,
    description: MaybeUnset[str | None] = UNSET,
    priority: MaybeUnset[int | None] = UNSET,
    task_type: MaybeUnset[str | None] = UNSET,
    assignee: MaybeUnset[str | None] = UNSET,
    claimed_by_session_id: MaybeUnset[str | None] = UNSET,
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
    allow_automation: MaybeUnset[bool | None] = UNSET,
    unattended: MaybeUnset[bool | None] = UNSET,
    yolo: MaybeUnset[bool | None] = UNSET,
    isolation: MaybeUnset[Isolation | str | None] = UNSET,
    assigned_agent: MaybeUnset[str | None] = UNSET,
    additional_skills: MaybeUnset[list[str] | None] = UNSET,
    **kwargs: Any,
) -> bool:
    """Validate a metadata-only update and dispatch to ``update_task``.

    LocalTaskManager.update_task delegates here so claim/session and
    lifecycle state cannot be mutated through the generic update surface.
    Stage and ownership transitions must use the dedicated transition
    helpers instead.

    Returns the same ``parent_changed`` flag as ``update_task``.
    Raises ``ValueError`` if any legacy state field or stage/ownership
    field is supplied.
    """
    legacy_stage_key = "lifecycle_" + "stage"
    legacy_state_fields = sorted({"status", "lifecycle", legacy_stage_key} & set(kwargs))
    blocked_fields = [
        field_name
        for field_name, value in (
            ("assignee", assignee),
            ("claimed_by_session_id", claimed_by_session_id),
            ("closed_reason", closed_reason),
            ("closed_at", closed_at),
            ("closed_in_session_id", closed_in_session_id),
            ("closed_commit_sha", closed_commit_sha),
            ("escalated_at", escalated_at),
            ("escalation_reason", escalation_reason),
        )
        if value is not UNSET
    ]
    if legacy_state_fields or blocked_fields:
        blocked_display = ", ".join([*legacy_state_fields, *blocked_fields])
        if legacy_state_fields and not blocked_fields:
            field_class = "legacy state fields"
            transition_hint = (
                "Use start_stage, submit_for_review, approve_review, reject_review, "
                "fail_stage, close_task, reopen_task, or escalate_task instead."
            )
        else:
            field_class = "stage or ownership fields"
            transition_hint = (
                "Use claim_task, release_task_claim, start_stage, submit_for_review, "
                "approve_review, reject_review, fail_stage, escalate_task, "
                "de_escalate_task, close_task, or reopen_task instead."
            )
        raise ValueError(
            f"LocalTaskManager.update_task does not allow {field_class}. "
            f"{transition_hint} Blocked fields: {blocked_display}"
        )

    return update_task(
        db,
        task_id=task_id,
        title=title,
        description=description,
        priority=priority,
        task_type=task_type,
        assignee=assignee,
        claimed_by_session_id=claimed_by_session_id,
        labels=labels,
        parent_task_id=parent_task_id,
        closed_reason=closed_reason,
        closed_at=closed_at,
        closed_in_session_id=closed_in_session_id,
        closed_commit_sha=closed_commit_sha,
        validation_status=validation_status,
        validation_feedback=validation_feedback,
        category=category,
        validation_criteria=validation_criteria,
        validation_fail_count=validation_fail_count,
        dispatch_failure_count=dispatch_failure_count,
        escalated_at=escalated_at,
        escalation_reason=escalation_reason,
        github_issue_number=github_issue_number,
        github_pr_number=github_pr_number,
        github_repo=github_repo,
        linear_issue_id=linear_issue_id,
        linear_team_id=linear_team_id,
        validation_override_reason=validation_override_reason,
        allow_automation=allow_automation,
        unattended=unattended,
        yolo=yolo,
        isolation=isolation,
        assigned_agent=assigned_agent,
        additional_skills=additional_skills,
    )
