"""Task aggregate operations.

This module provides aggregate operations for task counts and statistics:
- count_tasks: Count tasks with optional filters
- count_by_status: Count tasks grouped by status
- count_ready_tasks: Count tasks ready to work on
- count_blocked_tasks: Count tasks blocked by dependencies
"""

from typing import Any

from gobby.storage.database import DatabaseProtocol


def _current_stage_join_sql(task_alias: str = "t", *, join_type: str = "LEFT JOIN") -> str:
    return f"""
    {join_type} task_stage_states current_stage
      ON current_stage.task_id = {task_alias}.id
     AND current_stage.state != 'done'
     AND current_stage.position = (
         SELECT MIN(stage_scan.position)
           FROM task_stage_states stage_scan
          WHERE stage_scan.task_id = {task_alias}.id
            AND stage_scan.state != 'done'
     )
    """


def _projected_status_sql(task_alias: str = "t") -> str:
    return (
        "CASE "
        f"WHEN {task_alias}.closed_at IS NOT NULL THEN 'closed' "
        f"WHEN {task_alias}.escalated_at IS NOT NULL "
        f"OR COALESCE({task_alias}.is_escalated, 0) = 1 THEN 'escalated' "
        "WHEN current_stage.state = 'ready' THEN 'open' "
        "WHEN current_stage.state IN ('in_progress', 'needs_review', 'review_approved') "
        "THEN current_stage.state "
        "ELSE 'open' END"
    )


def _status_filter_sql(status: str | list[str] | None) -> tuple[str | None, list[Any]]:
    if not status:
        return None, []
    statuses = [status] if isinstance(status, str) else list(status)
    placeholders = ", ".join("?" for _ in statuses)
    return f"{_projected_status_sql()} IN ({placeholders})", statuses


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
    query = f"""
    SELECT COUNT(*) as count
      FROM tasks t
      {_current_stage_join_sql("t")}
     WHERE 1=1
    """
    params: list[Any] = []

    if project_id:
        query += " AND t.project_id = ?"
        params.append(project_id)
    if status:
        clause, clause_params = _status_filter_sql(status)
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
    projected_status = _projected_status_sql("t")
    query = f"""
    SELECT {projected_status} as status, COUNT(*) as count
      FROM tasks t
      {_current_stage_join_sql("t")}
    """
    params: list[Any] = []

    if project_id:
        query += " WHERE t.project_id = ?"
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
    {_current_stage_join_sql("t", join_type="JOIN")}
    WHERE {_not_closed_or_escalated_sql("t")}
    AND current_stage.state IN ('ready', 'in_progress')
    AND {_no_external_blocker_sql("t")}
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
    query = f"""
    SELECT COUNT(*) as count FROM tasks t
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

    result = db.fetchone(query, tuple(params))
    return result["count"] if result else 0
