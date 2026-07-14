"""Task aggregate operations."""

from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import json_array_contains_condition, newer_than_now_expr
from gobby.storage.tasks._models import task_type_filter_values
from gobby.storage.tasks._queries import _ready_tasks_cte_sql


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
    placeholders = ", ".join("%s" for _ in normalized)
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
        f"AND COALESCE({task_alias}.is_escalated, FALSE) IS FALSE"
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
              WITH RECURSIVE ancestors(ancestor_id, path, depth) AS (
                  SELECT blocker.parent_task_id,
                         ARRAY[blocker.id, blocker.parent_task_id],
                         1
                  WHERE blocker.parent_task_id IS NOT NULL
                  UNION ALL
                  SELECT p.parent_task_id, a.path || p.parent_task_id, a.depth + 1
                  FROM tasks p
                  JOIN ancestors a ON p.id = a.ancestor_id
                  WHERE p.parent_task_id IS NOT NULL
                    AND a.depth < 100
                    AND NOT p.parent_task_id = ANY(a.path)
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
    current_stage_state: str | list[str] | None = None,
    priority: int | None = None,
    claimed_by_session_id: str | None = None,
    claimed: bool | None = None,
    closed: bool | None = None,
    escalated: bool | None = None,
    task_type: str | None = None,
    label: str | None = None,
    parent_task_id: str | None = None,
    title_like: str | None = None,
    stages: list[str] | None = None,
    stage_state: str | None = None,
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
        query += " AND t.project_id = %s"
        params.append(project_id)
    if current_stage_state:
        clause, clause_params = _stage_state_filter_clause(current_stage_state)
        if clause:
            query += f" AND {clause}"
            params.extend(clause_params)
            if closed is None:
                query += " AND t.closed_at IS NULL"
    if priority is not None:
        query += " AND t.priority = %s"
        params.append(priority)
    if claimed_by_session_id:
        query += " AND t.claimed_by_session_id = %s"
        params.append(claimed_by_session_id)
    if claimed is True:
        query += " AND t.claimed_by_session_id IS NOT NULL"
    elif claimed is False:
        query += " AND t.claimed_by_session_id IS NULL"
    if closed is True:
        query += " AND t.closed_at IS NOT NULL"
    elif closed is False:
        query += " AND t.closed_at IS NULL"
    if escalated is True:
        query += " AND COALESCE(t.is_escalated, FALSE) IS TRUE"
    elif escalated is False:
        query += " AND COALESCE(t.is_escalated, FALSE) IS FALSE"
    if task_type:
        task_type_values = task_type_filter_values(task_type)
        placeholders = ", ".join("%s" for _ in task_type_values)
        query += f" AND t.task_type IN ({placeholders})"
        params.extend(task_type_values)
    if label:
        label_clause, label_params = json_array_contains_condition(db, "t.labels", label)
        query += f" AND {label_clause}"
        params.extend(label_params)
    if parent_task_id:
        query += " AND t.parent_task_id = %s"
        params.append(parent_task_id)
    if title_like:
        query += " AND t.title LIKE %s"
        params.append(f"%{title_like}%")
    if stages:
        placeholders = ", ".join("%s" for _ in stages)
        query += f"""
        AND EXISTS (
            SELECT 1
              FROM task_stage_states stage_filter
             WHERE stage_filter.task_id = t.id
               AND stage_filter.stage_name IN ({placeholders})
        """
        params.extend(stages)
        if stage_state is not None:
            query += " AND stage_filter.state = %s"
            params.append(stage_state)
        query += ")"

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
        query += " WHERE t.project_id = %s"
        params.append(project_id)

    query += " GROUP BY state_bucket"

    rows = db.fetchall(query, tuple(params))
    return {row["state_bucket"]: row["count"] for row in rows}


def count_ready_tasks(
    db: HubDatabase,
    project_id: str | None = None,
) -> int:
    """Count tasks that are ready to work on (open or in_progress) and not blocked.

    A task is ready if it has no external blocking dependencies and every task
    in its parent chain is also ready. In-progress tasks remain in the ready
    count because they represent active work.

    Args:
        db: Database protocol instance
        project_id: Optional project filter

    Returns:
        Count of ready tasks
    """
    # Match list_ready_tasks by materializing only tasks whose full parent chain is ready.
    query = f"""
    {_ready_tasks_cte_sql()}
    SELECT COUNT(*) as count FROM tasks t
    JOIN ready_tasks rt ON t.id = rt.id
    WHERE 1=1
    """
    params: list[Any] = []

    if project_id:
        query += " AND t.project_id = %s"
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
    closed_recent_sql = newer_than_now_expr(db, "closed_at", "%s", "hour")
    query = (
        f"SELECT COUNT(*) as count FROM tasks WHERE closed_at IS NOT NULL AND {closed_recent_sql}"
    )
    params: list[Any] = [hours]

    if project_id:
        query += " AND project_id = %s"
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
        OR COALESCE(t.is_escalated, FALSE) IS TRUE
        OR {_external_blocker_exists_sql("t")}
    )
    """
    params: list[Any] = []

    if project_id:
        query += " AND t.project_id = %s"
        params.append(project_id)

    result = db.fetchone(query, tuple(params))
    return result["count"] if result else 0
