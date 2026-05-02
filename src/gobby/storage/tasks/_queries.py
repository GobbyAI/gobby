"""Task query operations.

This module provides query operations for listing and filtering tasks:
- list_tasks: General task listing with filters
- list_ready_tasks: Tasks ready to work on (not blocked)
- list_blocked_tasks: Tasks blocked by dependencies
- list_workflow_tasks: Tasks associated with a workflow
"""

from typing import Any

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._blocking import hydrate_task_blocking_state
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._ordering import order_tasks_hierarchically
from gobby.storage.tasks._state_sql import status_filter_sql
from gobby.tasks.state_semantics import normalize_lifecycle_stage


def _lifecycle_stage_filter_sql(
    lifecycle_stage: str | list[str] | None,
) -> tuple[str | None, list[Any]]:
    """Build a canonical lifecycle-stage filter clause and params."""
    if not lifecycle_stage:
        return None, []

    raw_values = [lifecycle_stage] if isinstance(lifecycle_stage, str) else list(lifecycle_stage)
    include_open = False
    normalized_values: list[str] = []

    for raw_value in raw_values:
        normalized = normalize_lifecycle_stage(raw_value)
        if normalized is None:
            include_open = True
        elif normalized not in normalized_values:
            normalized_values.append(normalized)

    clauses: list[str] = []
    params: list[Any] = []
    if normalized_values:
        placeholders = ", ".join("?" for _ in normalized_values)
        clauses.append(f"lifecycle_stage IN ({placeholders})")
        params.extend(normalized_values)
    if include_open:
        clauses.append("lifecycle_stage IS NULL")

    if not clauses:
        return None, []
    return "(" + " OR ".join(clauses) + ")", params


def _current_stage_join_sql(task_alias: str = "t") -> str:
    return f"""
    JOIN task_stage_states current_stage
      ON current_stage.task_id = {task_alias}.id
     AND current_stage.state != 'done'
     AND current_stage.position = (
         SELECT MIN(stage_scan.position)
           FROM task_stage_states stage_scan
          WHERE stage_scan.task_id = {task_alias}.id
            AND stage_scan.state != 'done'
     )
    """


def _not_closed_or_escalated_sql(task_alias: str = "t") -> str:
    return (
        f"{task_alias}.closed_at IS NULL "
        f"AND {task_alias}.escalated_at IS NULL "
        f"AND COALESCE({task_alias}.is_escalated, 0) = 0"
    )


def _external_blocker_exists_sql(task_alias: str = "t") -> str:
    return f"""
    EXISTS (
        SELECT 1 FROM task_dependencies d
        JOIN tasks blocker ON d.depends_on = blocker.id
        WHERE d.task_id = {task_alias}.id
          AND d.dep_type = 'blocks'
          AND blocker.closed_at IS NULL
          AND NOT EXISTS (
              WITH RECURSIVE ancestors AS (
                  SELECT blocker.parent_task_id AS ancestor_id
                  UNION ALL
                  SELECT p.parent_task_id
                  FROM tasks p
                  JOIN ancestors a ON p.id = a.ancestor_id
                  WHERE p.parent_task_id IS NOT NULL
              )
              SELECT 1 FROM ancestors WHERE ancestor_id = {task_alias}.id
          )
    )
    """


def _no_external_blocker_sql(task_alias: str = "t") -> str:
    return f"NOT {_external_blocker_exists_sql(task_alias)}"


def list_tasks(
    db: DatabaseProtocol,
    project_id: str | None = None,
    status: str | list[str] | None = None,
    lifecycle_stage: str | list[str] | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    claimed_by_session_id: str | None = None,
    claimed: bool | None = None,
    closed: bool | None = None,
    task_type: str | None = None,
    label: str | None = None,
    parent_task_id: str | None = None,
    title_like: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "hierarchy",
    sort_order: str = "asc",
) -> list[Task]:
    """List tasks with filtering.

    Args:
        db: Database protocol instance
        project_id: Filter by project
        status: Filter by status. Can be a single status string, a list of statuses,
            or None to include all statuses.
        lifecycle_stage: Filter by canonical lifecycle stage (`open`, `in_progress`,
            `needs_review`, `review_approved`) independent of closed/escalated state.
        priority: Filter by priority
        assignee: Filter by assignee
        claimed_by_session_id: Filter by canonical owning session
        claimed: Filter by whether canonical ownership exists
        closed: Filter by canonical closed state
        task_type: Filter by task type
        label: Filter by label
        parent_task_id: Filter by parent task
        title_like: Filter by title (partial match)
        limit: Maximum tasks to return
        offset: Pagination offset
        sort_by: Ordering strategy. "hierarchy" preserves parent/child ordering;
            "updated_at" and "created_at" sort chronologically; "priority"
            sorts by task priority (lower value = higher priority).
        sort_order: "asc" or "desc" for non-hierarchical sorts.

    Results are ordered hierarchically: parents appear before their children,
    with siblings sorted by priority ASC, then created_at ASC.
    """
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []

    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)
    if status:
        clause, clause_params = status_filter_sql(status)
        if clause:
            query += f" AND {clause}"
            params.extend(clause_params)
    if lifecycle_stage:
        clause, clause_params = _lifecycle_stage_filter_sql(lifecycle_stage)
        if clause:
            query += f" AND {clause}"
            params.extend(clause_params)
    if priority:
        query += " AND priority = ?"
        params.append(priority)
    if assignee:
        query += " AND assignee = ?"
        params.append(assignee)
    if claimed_by_session_id:
        query += " AND claimed_by_session_id = ?"
        params.append(claimed_by_session_id)
    if claimed is True:
        query += " AND claimed_by_session_id IS NOT NULL"
    elif claimed is False:
        query += " AND claimed_by_session_id IS NULL"
    if closed is True:
        query += " AND closed_at IS NOT NULL"
    elif closed is False:
        query += " AND closed_at IS NULL"
    if task_type:
        query += " AND task_type = ?"
        params.append(task_type)
    if label:
        # tasks.labels is a JSON list. We use json_each to find if the label is in the list.
        query += " AND EXISTS (SELECT 1 FROM json_each(tasks.labels) WHERE value = ?)"
        params.append(label)
    if parent_task_id:
        query += " AND parent_task_id = ?"
        params.append(parent_task_id)
    if title_like:
        query += " AND title LIKE ?"
        params.append(f"%{title_like}%")

    valid_sorts = {
        "hierarchy": "priority ASC, created_at ASC",
        "updated_at": "updated_at",
        "created_at": "created_at",
        "priority": "priority",
    }
    order_clause = valid_sorts.get(sort_by, valid_sorts["hierarchy"])
    direction = "DESC" if sort_order.lower() == "desc" else "ASC"
    if sort_by == "hierarchy":
        query += f" ORDER BY {order_clause} LIMIT ? OFFSET ?"
    else:
        query += (
            f" ORDER BY {order_clause} {direction}, priority ASC, created_at DESC LIMIT ? OFFSET ?"
        )
    params.extend([limit, offset])

    rows = db.fetchall(query, tuple(params))
    tasks = [Task.from_row(row) for row in rows]
    hydrate_task_blocking_state(db, tasks)

    if sort_by == "hierarchy":
        return order_tasks_hierarchically(tasks)
    return tasks


def list_ready_tasks(
    db: DatabaseProtocol,
    project_id: str | None = None,
    priority: int | None = None,
    task_type: str | None = None,
    assignee: str | None = None,
    parent_task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Task]:
    """List tasks that are ready to work on (open or in_progress) and not blocked.

    A task is ready if:
    1. It is open or in_progress
    2. It has no open blocking dependencies
    3. Its parent (if any) is also ready (recursive check up the chain)

    Note: in_progress tasks are included because they represent active work
    that should remain visible in the ready queue.

    Results are ordered hierarchically: parents appear before their children,
    with siblings sorted by priority ASC, then created_at ASC.

    Note: The limit is applied AFTER hierarchical ordering to ensure coherent
    tree structures. We fetch all ready tasks, order them hierarchically,
    then return the first N tasks in tree traversal order.
    """
    # Use recursive CTE to find tasks with ready parent chains
    query = f"""
    WITH RECURSIVE ready_tasks AS (
        -- Base case: open/in_progress tasks with no parent and no external blocking deps
        SELECT t.id FROM tasks t
        {_current_stage_join_sql("t")}
        WHERE {_not_closed_or_escalated_sql("t")}
        AND current_stage.state IN ('ready', 'in_progress')
        AND t.parent_task_id IS NULL
        AND {_no_external_blocker_sql("t")}

        UNION ALL

        -- Recursive case: open/in_progress tasks whose parent is ready and no external blocking deps
        SELECT t.id FROM tasks t
        JOIN ready_tasks rt ON t.parent_task_id = rt.id
        {_current_stage_join_sql("t")}
        WHERE {_not_closed_or_escalated_sql("t")}
        AND current_stage.state IN ('ready', 'in_progress')
        AND {_no_external_blocker_sql("t")}
    )
    SELECT t.* FROM tasks t
    JOIN ready_tasks rt ON t.id = rt.id
    WHERE 1=1
    """
    params: list[Any] = []

    if project_id:
        query += " AND t.project_id = ?"
        params.append(project_id)
    if priority:
        query += " AND t.priority = ?"
        params.append(priority)
    if task_type:
        query += " AND t.task_type = ?"
        params.append(task_type)
    if assignee:
        query += " AND t.assignee = ?"
        params.append(assignee)
    if parent_task_id:
        query += " AND t.parent_task_id = ?"
        params.append(parent_task_id)

    # Fetch all matching tasks (no SQL limit) so we can order hierarchically first
    internal_limit = 1000
    query += " ORDER BY t.priority ASC, t.created_at ASC LIMIT ?"
    params.append(internal_limit)

    rows = db.fetchall(query, tuple(params))
    tasks = [Task.from_row(row) for row in rows]
    hydrate_task_blocking_state(db, tasks)

    # Order hierarchically, then apply user's limit/offset
    ordered = order_tasks_hierarchically(tasks)
    return ordered[offset : offset + limit] if limit else ordered


def list_blocked_tasks(
    db: DatabaseProtocol,
    project_id: str | None = None,
    parent_task_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Task]:
    """List tasks that are blocked by at least one open blocking dependency.

    Only considers "external" blockers - excludes parent tasks being blocked
    by their own descendants (which is a "completion" block, not a "work" block).

    Results are ordered hierarchically: parents appear before their children,
    with siblings sorted by priority ASC, then created_at ASC.

    Note: The limit is applied AFTER hierarchical ordering to ensure coherent
    tree structures.
    """
    query = f"""
    SELECT t.* FROM tasks t
    WHERE t.closed_at IS NULL
    AND (
        t.escalated_at IS NOT NULL
        OR COALESCE(t.is_escalated, 0) = 1
        OR {_external_blocker_exists_sql("t")}
    )
    """
    params: list[Any] = []

    if project_id:
        query += " AND t.project_id = ?"
        params.append(project_id)
    if parent_task_id:
        query += " AND t.parent_task_id = ?"
        params.append(parent_task_id)

    # Fetch all matching tasks (no SQL limit) so we can order hierarchically first
    internal_limit = 1000
    query += " ORDER BY t.priority ASC, t.created_at ASC LIMIT ?"
    params.append(internal_limit)

    rows = db.fetchall(query, tuple(params))
    tasks = [Task.from_row(row) for row in rows]
    hydrate_task_blocking_state(db, tasks)

    # Order hierarchically, then apply user's limit/offset
    ordered = order_tasks_hierarchically(tasks)
    return ordered[offset : offset + limit] if limit else ordered
