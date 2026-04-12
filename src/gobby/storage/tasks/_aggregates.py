"""Task aggregate operations.

This module provides aggregate operations for task counts and statistics:
- count_tasks: Count tasks with optional filters
- count_by_status: Count tasks grouped by status
- count_ready_tasks: Count tasks ready to work on
- count_blocked_tasks: Count tasks blocked by dependencies
"""

from typing import Any

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._state_sql import canonical_status_case, is_ready_sql, status_filter_sql


def count_tasks(
    db: DatabaseProtocol,
    project_id: str | None = None,
    status: str | None = None,
) -> int:
    """Count tasks with optional filters.

    Args:
        db: Database protocol instance
        project_id: Filter by project
        status: Filter by status

    Returns:
        Count of matching tasks
    """
    query = "SELECT COUNT(*) as count FROM tasks WHERE 1=1"
    params: list[Any] = []

    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)
    if status:
        clause, clause_params = status_filter_sql(status)
        if clause:
            query += f" AND {clause}"
            params.extend(clause_params)

    result = db.fetchone(query, tuple(params))
    return result["count"] if result else 0


def count_by_status(
    db: DatabaseProtocol,
    project_id: str | None = None,
) -> dict[str, int]:
    """Count tasks grouped by status.

    Args:
        db: Database protocol instance
        project_id: Optional project filter

    Returns:
        Dictionary mapping status to count
    """
    projected_status = canonical_status_case()
    query = f"SELECT {projected_status} as status, COUNT(*) as count FROM tasks"
    params: list[Any] = []

    if project_id:
        query += " WHERE project_id = ?"
        params.append(project_id)

    query += " GROUP BY status"

    rows = db.fetchall(query, tuple(params))
    return {row["status"]: row["count"] for row in rows}


def count_ready_tasks(
    db: DatabaseProtocol,
    project_id: str | None = None,
) -> int:
    """Count tasks that are ready (open and not blocked).

    A task is ready if it is open and has no external blocking dependencies.
    Excludes parent tasks blocked by their own descendants (completion block, not work block).
    In-progress tasks are not counted as "ready" — they are already being worked on.

    Args:
        db: Database protocol instance
        project_id: Optional project filter

    Returns:
        Count of ready tasks
    """
    # Uses the same descendant-aware predicate as list_ready_tasks.
    # The is_descendant_of check uses a recursive CTE to walk up the blocker's
    # ancestor chain and check if the blocked task (t.id) appears anywhere.
    query = f"""
    SELECT COUNT(*) as count FROM tasks t
    WHERE {is_ready_sql('t')}
    AND NOT EXISTS (
        SELECT 1 FROM task_dependencies d
        JOIN tasks blocker ON d.depends_on = blocker.id
        WHERE d.task_id = t.id
          AND d.dep_type = 'blocks'
          AND blocker.closed_at IS NULL
          -- Exclude ancestor blocked by any descendant (completion block, not work block)
          -- Check if t.id appears anywhere in blocker's ancestor chain
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

    result = db.fetchone(query, tuple(params))
    return result["count"] if result else 0


def count_closed_since(
    db: DatabaseProtocol,
    hours: int = 24,
    project_id: str | None = None,
) -> int:
    """Count tasks closed within the last N hours.

    Args:
        db: Database protocol instance
        hours: Time window in hours
        project_id: Optional project filter

    Returns:
        Count of recently closed tasks
    """
    query = (
        "SELECT COUNT(*) as count FROM tasks "
        "WHERE closed_at IS NOT NULL "
        "AND closed_at >= datetime('now', ?)"
    )
    params: list[Any] = [f"-{hours} hours"]

    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)

    result = db.fetchone(query, tuple(params))
    return result["count"] if result else 0


def count_blocked_tasks(
    db: DatabaseProtocol,
    project_id: str | None = None,
) -> int:
    """Count tasks that are blocked by at least one external blocking dependency.

    Excludes parent tasks blocked by their own descendants (completion block, not work block).

    Args:
        db: Database protocol instance
        project_id: Optional project filter

    Returns:
        Count of blocked tasks
    """
    # Uses the same descendant-aware predicate as list_ready_tasks.
    # The is_descendant_of check uses a recursive CTE to walk up the blocker's
    # ancestor chain and check if the blocked task (t.id) appears anywhere.
    query = """
    SELECT COUNT(*) as count FROM tasks t
    WHERE t.closed_at IS NULL
    AND EXISTS (
        SELECT 1 FROM task_dependencies d
        JOIN tasks blocker ON d.depends_on = blocker.id
        WHERE d.task_id = t.id
          AND d.dep_type = 'blocks'
          AND blocker.closed_at IS NULL
          -- Exclude ancestor blocked by any descendant (completion block, not work block)
          -- Check if t.id appears anywhere in blocker's ancestor chain
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

    result = db.fetchone(query, tuple(params))
    return result["count"] if result else 0
