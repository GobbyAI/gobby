"""Task query operations.

This module provides query operations for listing and filtering tasks:
- list_tasks: General task listing with filters
- list_ready_tasks: Tasks ready to work on (not blocked)
- list_blocked_tasks: Tasks blocked by dependencies
- list_workflow_tasks: Tasks associated with a workflow
"""

from typing import Any

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._models import Task
from gobby.storage.tasks._ordering import order_tasks_hierarchically
from gobby.storage.tasks._state_sql import is_ready_sql, status_filter_sql


def list_tasks(
    db: DatabaseProtocol,
    project_id: str | None = None,
    status: str | list[str] | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    claimed_by_session_id: str | None = None,
    task_type: str | None = None,
    label: str | None = None,
    parent_task_id: str | None = None,
    title_like: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Task]:
    """List tasks with filtering.

    Args:
        db: Database protocol instance
        project_id: Filter by project
        status: Filter by status. Can be a single status string, a list of statuses,
            or None to include all statuses.
        priority: Filter by priority
        assignee: Filter by assignee
        claimed_by_session_id: Filter by canonical owning session
        task_type: Filter by task type
        label: Filter by label
        parent_task_id: Filter by parent task
        title_like: Filter by title (partial match)
        limit: Maximum tasks to return
        offset: Pagination offset

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
    if priority:
        query += " AND priority = ?"
        params.append(priority)
    if assignee:
        query += " AND assignee = ?"
        params.append(assignee)
    if claimed_by_session_id:
        query += " AND claimed_by_session_id = ?"
        params.append(claimed_by_session_id)
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

    # Fetch with base ordering, then apply hierarchical sort in Python
    query += " ORDER BY priority ASC, created_at ASC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = db.fetchall(query, tuple(params))
    tasks = [Task.from_row(row) for row in rows]

    # Bulk fetch dependencies for these tasks to support topological sort
    if tasks:
        task_ids = [t.id for t in tasks]
        placeholders = ", ".join("?" for _ in task_ids)
        dep_rows = db.fetchall(
            f"SELECT task_id, depends_on FROM task_dependencies WHERE dep_type = 'blocks' AND task_id IN ({placeholders})",  # nosec B608
            tuple(task_ids),
        )

        # Map by task_id -> set of blockers
        blockers_map: dict[str, set[str]] = {}
        for row in dep_rows:
            tid = row["task_id"]
            blocker = row["depends_on"]
            if tid not in blockers_map:
                blockers_map[tid] = set()
            blockers_map[tid].add(blocker)

        # Populate task objects
        for task in tasks:
            if task.id in blockers_map:
                task.blocked_by = blockers_map[task.id]

    return order_tasks_hierarchically(tasks)


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
        WHERE {is_ready_sql('t')}
        AND t.parent_task_id IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on = blocker.id
            WHERE d.task_id = t.id
              AND d.dep_type = 'blocks'
              AND blocker.closed_at IS NULL
              -- Exclude ancestor blocked by any descendant (completion block, not work block)
              AND NOT EXISTS (
                  WITH RECURSIVE ancestors AS (
                      SELECT blocker.parent_task_id AS ancestor_id
                      UNION ALL
                      SELECT p.parent_task_id
                      FROM tasks p
                      JOIN ancestors a ON p.id = a.ancestor_id
                      WHERE p.parent_task_id IS NOT NULL
                  )
                  SELECT 1 FROM ancestors WHERE ancestor_id = t.id
              )
        )

        UNION ALL

        -- Recursive case: open/in_progress tasks whose parent is ready and no external blocking deps
        SELECT t.id FROM tasks t
        JOIN ready_tasks rt ON t.parent_task_id = rt.id
        WHERE {is_ready_sql('t')}
        AND NOT EXISTS (
            SELECT 1 FROM task_dependencies d
            JOIN tasks blocker ON d.depends_on = blocker.id
            WHERE d.task_id = t.id
              AND d.dep_type = 'blocks'
              AND blocker.closed_at IS NULL
              -- Exclude ancestor blocked by any descendant (completion block, not work block)
              AND NOT EXISTS (
                  WITH RECURSIVE ancestors AS (
                      SELECT blocker.parent_task_id AS ancestor_id
                      UNION ALL
                      SELECT p.parent_task_id
                      FROM tasks p
                      JOIN ancestors a ON p.id = a.ancestor_id
                      WHERE p.parent_task_id IS NOT NULL
                  )
                  SELECT 1 FROM ancestors WHERE ancestor_id = t.id
              )
        )
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
    query = """
    SELECT t.* FROM tasks t
    WHERE t.closed_at IS NULL
    AND EXISTS (
        SELECT 1 FROM task_dependencies d
        JOIN tasks blocker ON d.depends_on = blocker.id
        WHERE d.task_id = t.id
          AND d.dep_type = 'blocks'
          AND blocker.closed_at IS NULL
          -- Exclude ancestor blocked by any descendant (completion block, not work block)
          AND NOT EXISTS (
              WITH RECURSIVE ancestors AS (
                  SELECT blocker.parent_task_id AS ancestor_id
                  UNION ALL
                  SELECT p.parent_task_id
                  FROM tasks p
                  JOIN ancestors a ON p.id = a.ancestor_id
                  WHERE p.parent_task_id IS NOT NULL
              )
              SELECT 1 FROM ancestors WHERE ancestor_id = t.id
          )
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

    # Order hierarchically, then apply user's limit/offset
    ordered = order_tasks_hierarchically(tasks)
    return ordered[offset : offset + limit] if limit else ordered
