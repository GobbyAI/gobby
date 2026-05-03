"""
Internal MCP tools for Hub (cross-project) queries.

Exposes functionality for:
- list_all_projects(): List all unique projects in hub database
- list_cross_project_tasks(state?): Query tasks across all projects
- list_cross_project_sessions(limit?): Recent sessions across all projects
- hub_stats(): Aggregate statistics from hub database

These tools query the hub database directly (not the project db).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.database import LocalDatabase

__all__ = ["create_hub_registry"]


def _current_stage_join_sql(task_alias: str = "t") -> str:
    return f"""
    LEFT JOIN task_stage_states current_stage
      ON current_stage.task_id = {task_alias}.id
     AND current_stage.state != 'done'
     AND current_stage.position = (
         SELECT MIN(stage_scan.position)
           FROM task_stage_states stage_scan
          WHERE stage_scan.task_id = {task_alias}.id
            AND stage_scan.state != 'done'
     )
    """


def _task_state_bucket_sql(task_alias: str = "t") -> str:
    return (
        "CASE "
        f"WHEN {task_alias}.closed_at IS NOT NULL THEN 'closed' "
        f"WHEN {task_alias}.escalated_at IS NOT NULL "
        f"OR COALESCE({task_alias}.is_escalated, 0) = 1 THEN 'escalated' "
        "WHEN current_stage.state = 'ready' THEN 'ready' "
        "WHEN current_stage.state IN ('in_progress', 'needs_review', 'review_approved') "
        "THEN current_stage.state "
        "ELSE 'ready' END"
    )


def _task_state_from_row(row: dict[str, Any]) -> dict[str, Any]:
    closed_at = row["closed_at"]
    escalated_at = row["escalated_at"]
    is_closed = bool(closed_at)
    is_escalated = not is_closed and bool(escalated_at or row["is_escalated"])
    owner_session_id = row["claimed_by_session_id"] or row["assignee"]
    current_stage = None
    if row["current_stage_name"]:
        current_stage = {
            "name": row["current_stage_name"],
            "display_name": row["current_stage_display_name"] or row["current_stage_name"],
            "category": row["current_stage_category"] or "",
            "state": row["current_stage_state"],
            "review_policy": row["current_stage_review_policy"] or "none",
            "updated_at": row["current_stage_updated_at"],
        }
    stage_state = row["current_stage_state"]
    active_stage = stage_state in {"ready", "in_progress", "needs_review", "review_approved"}
    return {
        "owner_session_id": owner_session_id,
        "current_stage": current_stage,
        "is_claimed": bool(
            owner_session_id and active_stage and not is_closed and not is_escalated
        ),
        "is_closed": is_closed,
        "is_escalated": is_escalated,
        "is_blocked": bool(row["is_blocked"]),
        "is_merge_ready": bool(
            stage_state == "review_approved" and not is_closed and not is_escalated
        ),
        "closed_at": closed_at,
        "closed_reason": row["closed_reason"],
        "closed_in_session_id": row["closed_in_session_id"],
        "closed_commit_sha": row["closed_commit_sha"],
        "escalated_at": escalated_at,
        "escalation_reason": row["escalation_reason"],
    }


def create_hub_registry(
    hub_db_path: Path,
) -> InternalToolRegistry:
    """
    Create a hub query tool registry with cross-project tools.

    Args:
        hub_db_path: Path to the hub database file

    Returns:
        InternalToolRegistry with hub query tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-hub",
        description="Hub (cross-project) queries and system info - get_machine_id, list_all_projects (for cross-project task creation), list_cross_project_tasks, list_cross_project_sessions, hub_stats",
    )

    def _get_hub_db() -> LocalDatabase | None:
        """Get hub database connection if it exists."""
        if not hub_db_path.exists():
            return None
        return LocalDatabase(hub_db_path)

    @registry.tool(
        name="get_machine_id",
        description="Get the daemon's machine identifier. Use this from sandboxed agents that cannot read ~/.gobby/machine_id directly.",
    )
    async def get_machine_id() -> dict[str, Any]:
        """
        Get the machine identifier used by this Gobby daemon.

        The machine_id is stored in ~/.gobby/machine_id and is generated
        once on first daemon run. This tool provides read-only access to
        the daemon's authoritative machine_id.

        Returns:
            Dict with machine_id or error if not found.
        """
        from gobby.utils.machine_id import get_machine_id as _get_machine_id

        machine_id = _get_machine_id()
        if machine_id:
            return {"success": True, "machine_id": machine_id}

        return {
            "success": False,
            "error": "machine_id not found - daemon may not have initialized properly",
        }

    @registry.tool(
        name="list_all_projects",
        description="List all initialized gobby projects with names and repo paths. Use project names with create_task(project='name') for cross-project task creation.",
    )
    async def list_all_projects(
        include_system: bool = False,
    ) -> dict[str, Any]:
        """
        List all initialized gobby projects.

        Returns project names and repo paths from the projects table.
        Use project names with create_task(project="name") for cross-project
        task creation.

        Args:
            include_system: Include system projects (_orphaned, _migrated, _personal, _global)
        """
        hub_db = _get_hub_db()
        if hub_db is None:
            return {"success": False, "error": f"Hub database not found: {hub_db_path}"}

        try:
            rows = await asyncio.to_thread(
                hub_db.fetchall,
                """
                SELECT p.id, p.name, p.repo_path,
                       COUNT(DISTINCT t.id) as task_count,
                       COUNT(DISTINCT s.id) as session_count
                FROM projects p
                LEFT JOIN tasks t ON t.project_id = p.id
                LEFT JOIN sessions s ON s.project_id = p.id
                WHERE p.deleted_at IS NULL
                GROUP BY p.id, p.name, p.repo_path
                ORDER BY p.name
                """,
            )
            projects = [
                {
                    "project_id": r["id"],
                    "name": r["name"],
                    "repo_path": r["repo_path"],
                    "task_count": r["task_count"],
                    "session_count": r["session_count"],
                }
                for r in rows
            ]
            if not include_system:
                system_prefixes = ("_orphaned", "_migrated", "_personal", "_global")
                projects = [p for p in projects if not p["name"].startswith(system_prefixes)]
            return {
                "success": True,
                "project_count": len(projects),
                "projects": projects,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="list_cross_project_tasks",
        description="Query tasks across all projects in the hub database.",
    )
    async def list_cross_project_tasks(
        state: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        List tasks across all projects in the hub.

        Args:
            state: Optional projected state filter (ready, closed, in_progress, needs_review)
            limit: Maximum number of tasks to return (default 50)
        """
        hub_db = _get_hub_db()
        if hub_db is None:
            return {"success": False, "error": f"Hub database not found: {hub_db_path}"}

        try:
            state_bucket = _task_state_bucket_sql("t")
            where_clause = ""
            params: tuple[Any, ...]
            if state:
                where_clause = f"WHERE {state_bucket} = ?"
                params = (state, limit)
            else:
                params = (limit,)
            rows = await asyncio.to_thread(
                hub_db.fetchall,
                f"""
                SELECT t.id, t.project_id, t.title, t.task_type, t.priority,
                       t.created_at, t.updated_at, t.assignee, t.claimed_by_session_id,
                       t.closed_at, t.closed_reason, t.closed_in_session_id,
                       t.closed_commit_sha, t.escalated_at, t.escalation_reason,
                       COALESCE(t.is_escalated, 0) as is_escalated,
                       current_stage.stage_name as current_stage_name,
                       current_stage.state as current_stage_state,
                       current_stage.review_policy as current_stage_review_policy,
                       current_stage.updated_at as current_stage_updated_at,
                       registry.display_label as current_stage_display_name,
                       registry.category as current_stage_category,
                       EXISTS (
                           SELECT 1
                             FROM task_dependencies td
                             JOIN tasks blocker ON td.depends_on = blocker.id
                            WHERE td.task_id = t.id
                              AND td.dep_type = 'blocks'
                              AND blocker.closed_at IS NULL
                       ) as is_blocked
                FROM tasks t
                {_current_stage_join_sql("t")}
                LEFT JOIN task_stages_registry registry
                  ON registry.name = current_stage.stage_name
                {where_clause}
                ORDER BY t.updated_at DESC
                LIMIT ?
                """,
                params,
            )

            tasks = [
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "title": row["title"],
                    "state": _task_state_from_row(dict(row)),
                    "task_type": row["task_type"],
                    "priority": row["priority"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

            return {
                "success": True,
                "count": len(tasks),
                "tasks": tasks,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="list_cross_project_sessions",
        description="List recent sessions across all projects in the hub database.",
    )
    async def list_cross_project_sessions(
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        List recent sessions across all projects in the hub.

        Args:
            limit: Maximum number of sessions to return (default 20)
        """
        hub_db = _get_hub_db()
        if hub_db is None:
            return {"success": False, "error": f"Hub database not found: {hub_db_path}"}

        try:
            rows = await asyncio.to_thread(
                hub_db.fetchall,
                """
                SELECT id, project_id, source, status, machine_id, created_at, updated_at
                FROM sessions
                WHERE source != 'system'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )

            sessions = [
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "source": row["source"],
                    "status": row["status"],
                    "machine_id": row["machine_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

            return {
                "success": True,
                "count": len(sessions),
                "sessions": sessions,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="hub_stats",
        description="Get aggregate statistics from the hub database.",
    )
    async def hub_stats() -> dict[str, Any]:
        """
        Get aggregate statistics from the hub database.

        Returns counts of projects, tasks, sessions, memories, etc.
        """
        hub_db = _get_hub_db()
        if hub_db is None:
            return {"success": False, "error": f"Hub database not found: {hub_db_path}"}

        def _collect_stats(db: LocalDatabase) -> dict[str, Any]:
            stats: dict[str, Any] = {}

            project_count_result = db.fetchone(
                """
                SELECT COUNT(DISTINCT project_id) as count
                FROM (
                    SELECT project_id FROM tasks WHERE project_id IS NOT NULL
                    UNION
                    SELECT project_id FROM sessions WHERE project_id IS NOT NULL AND source != 'system'
                )
                """
            )
            stats["project_count"] = project_count_result["count"] if project_count_result else 0

            task_stats = db.fetchall(
                f"""
                SELECT {_task_state_bucket_sql("t")} as task_state, COUNT(*) as count
                FROM tasks t
                {_current_stage_join_sql("t")}
                GROUP BY task_state
                """
            )
            stats["tasks"] = {
                "total": sum(row["count"] for row in task_stats),
                "by_state": {row["task_state"]: row["count"] for row in task_stats},
            }

            session_stats = db.fetchall(
                """
                SELECT status, COUNT(*) as count
                FROM sessions
                WHERE source != 'system'
                GROUP BY status
                """
            )
            stats["sessions"] = {
                "total": sum(row["count"] for row in session_stats),
                "by_status": {row["status"]: row["count"] for row in session_stats},
            }

            try:
                memory_count = db.fetchone("SELECT COUNT(*) as count FROM memories")
                stats["memories"] = memory_count["count"] if memory_count else 0
            except Exception:
                stats["memories"] = 0

            return stats

        try:
            stats = await asyncio.to_thread(_collect_stats, hub_db)
            return {"success": True, "stats": stats}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return registry
