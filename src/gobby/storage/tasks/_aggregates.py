"""Task aggregate operations."""

from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import newer_than_now_expr


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


def _stage_state_filter_clause(
    current_stage_state: str | list[str] | None,
) -> tuple[str | None, list[Any]]:
    if not current_stage_state:
        return None, []
    states = (
        [current_stage_state] if isinstance(current_stage_state, str) else list(current_stage_state)
    )
    normalized = [str(state).strip().lower().replace("-", "_") for state in states]
    placeholders = ", ".join("?" for _ in normalized)
    clauses = [f"current_stage.state IN ({placeholders})"]
    if "ready" in normalized:
        clauses.append(
            """
            NOT EXISTS (
                SELECT 1 FROM task_stage_states stage_any WHERE stage_any.task_id = t.id
            )
            """
        )
    return f"({' OR '.join(clauses)})", normalized


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
    db: HubDatabase,
    project_id: str | None = None,
    current_stage_state: str | None = None,
) -> int:
    """Count tasks with optional filters.

    Args:
        db: Database protocol instance
        project_id: Filter by project
        current_stage_state: Filter by current stage state

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
    if current_stage_state:
        clause, clause_params = _stage_state_filter_clause(current_stage_state)
        if clause:
            query += f" AND {clause}"
            params.extend(clause_params)
            query += " AND t.closed_at IS NULL"

    result = db.fetchone(query, tuple(params))
    return result["count"] if result else 0


def count_by_state(
    db: HubDatabase,
    project_id: str | None = None,
) -> dict[str, int]:
    """Count tasks grouped by canonical state bucket.

    Args:
        db: Database protocol instance
        project_id: Optional project filter

    Returns:
        Dictionary mapping state bucket to count
    """
    query = """
    SELECT t.state_bucket, COUNT(*) as count
      FROM tasks t
    """
    params: list[Any] = []

    if project_id:
        query += " WHERE t.project_id = ?"
        params.append(project_id)

    query += " GROUP BY state_bucket"

    rows = db.fetchall(query, tuple(params))
    return {row["state_bucket"]: row["count"] for row in rows}


def count_ready_tasks(
    db: HubDatabase,
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
    {_current_stage_join_sql("t")}
    WHERE {_not_closed_or_escalated_sql("t")}
    AND (
        current_stage.state IN ('ready', 'in_progress')
        OR NOT EXISTS (
            SELECT 1 FROM task_stage_states stage_any WHERE stage_any.task_id = t.id
        )
    )
    AND {_no_external_blocker_sql("t")}
    """
    params: list[Any] = []

    if project_id:
        query += " AND t.project_id = ?"
        params.append(project_id)

    result = db.fetchone(query, tuple(params))
    return result["count"] if result else 0


def count_closed_since(
    db: HubDatabase,
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
    closed_recent_sql = newer_than_now_expr(db, "closed_at", "?", "hour")
    query = (
        f"SELECT COUNT(*) as count FROM tasks WHERE closed_at IS NOT NULL AND {closed_recent_sql}"
    )
    params: list[Any] = [hours]

    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)

    result = db.fetchone(query, tuple(params))
    return result["count"] if result else 0


def count_blocked_tasks(
    db: HubDatabase,
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
