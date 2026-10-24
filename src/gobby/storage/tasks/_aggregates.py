"""Task aggregate operations."""

from datetime import datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import json_array_contains_condition, newer_than_now_expr
from gobby.storage.tasks._models import task_type_filter_values
from gobby.storage.tasks._queries import (
    _candidate_message_sql,
    _ready_tasks_cte_sql,
    projected_task_state_sql,
)
from gobby.storage.tasks._read import _escape_like_pattern
from gobby.tasks.state_semantics import validate_task_state


def count_tasks(
    db: HubDatabase,
    project_id: str | None = None,
    priority: int | None = None,
    claimed_by_session_id: str | None = None,
    claimed: bool | None = None,
    closed: bool | None = None,
    escalated: bool | None = None,
    task_type: str | None = None,
    label: str | None = None,
    labels: list[str] | None = None,
    parent_task_id: str | None = None,
    title_like: str | None = None,
    updated_since: datetime | None = None,
    candidate: bool | None = None,
    state: str | None = None,
) -> int:
    """Count tasks with optional filters.

    Args:
        db: Database protocol instance
        project_id: Filter by project

    Returns:
        Count of matching tasks
    """
    query = """
    SELECT COUNT(*) as count
      FROM tasks t
     WHERE 1=1
    """
    params: list[Any] = []

    if state is not None:
        query += f" AND ({projected_task_state_sql('t')}) = %s"
        params.append(validate_task_state(state))

    if project_id:
        query += " AND t.project_id = %s"
        params.append(project_id)
    if updated_since is not None:
        query += " AND t.updated_at >= %s"
        params.append(updated_since)
    if candidate is not None:
        query += f""" AND {"NOT " if not candidate else ""}EXISTS (
            SELECT 1 FROM ({_candidate_message_sql()}) events
            WHERE events.project_id = t.project_id
              AND events.seq_text = t.seq_num::text
        )"""
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
    if labels is not None:
        query += " AND t.labels ?| %s::text[]"
        params.append(labels)
    if parent_task_id:
        query += " AND t.parent_task_id = %s"
        params.append(parent_task_id)
    if title_like:
        query += " AND t.title LIKE %s ESCAPE '\\'"
        params.append(f"%{_escape_like_pattern(title_like)}%")

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
    query = f"""
    SELECT ({projected_task_state_sql("t")}) AS projected_state, COUNT(*) as count
      FROM tasks t
    """
    params: list[Any] = []

    if project_id:
        query += " WHERE t.project_id = %s"
        params.append(project_id)

    query += " GROUP BY projected_state"

    rows = db.fetchall(query, tuple(params))
    return {row["projected_state"]: row["count"] for row in rows}


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
    parent_task_id: str | None = None,
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
    WHERE ({projected_task_state_sql("t")}) = 'blocked'
    """
    params: list[Any] = []

    if project_id:
        query += " AND t.project_id = %s"
        params.append(project_id)

    if parent_task_id:
        query += " AND t.parent_task_id = %s"
        params.append(parent_task_id)

    result = db.fetchone(query, tuple(params))
    return result["count"] if result else 0
