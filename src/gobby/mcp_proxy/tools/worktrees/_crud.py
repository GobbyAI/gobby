"""Read-only worktree tools: get, list, stats, and task lookup."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.mcp_proxy.tools.worktrees._helpers import resolve_project_context
from gobby.mcp_proxy.tools.worktrees._merge_state import worktree_dict_with_git_merge_state

logger = logging.getLogger(__name__)


def create_crud_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with read-only worktree tools.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with get/list/stats/task-lookup tools
    """
    registry = InternalToolRegistry(
        name="gobby-worktrees-crud",
        description="Worktree read operations",
    )

    @registry.tool(
        name="get_worktree",
        description="Get details of a specific worktree.",
    )
    async def get_worktree(worktree_id: str) -> dict[str, Any]:
        """Get worktree details by ID.

        Args:
            worktree_id: The worktree ID.

        Returns:
            Dict with full worktree details.
        """
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        git_status = None
        if ctx.git_manager and Path(worktree.worktree_path).exists():
            status = ctx.git_manager.get_worktree_status(worktree.worktree_path)
            if status:
                git_status = {
                    "has_uncommitted_changes": status.has_uncommitted_changes,
                    "commits_ahead": status.ahead,
                    "commits_behind": status.behind,
                    "current_branch": status.branch,
                }

        return {
            "success": True,
            "worktree": worktree_dict_with_git_merge_state(worktree, ctx.git_manager),
            "git_status": git_status,
        }

    @registry.tool(
        name="list_worktrees",
        description="List worktrees with optional filters. Accepts #N, N, UUID, or prefix for agent_session_id.",
    )
    async def list_worktrees(
        status: str | None = None,
        agent_session_id: str | None = None,
        limit: int | str = 50,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """List worktrees with optional filters.

        Args:
            status: Filter by status (active, stale, merged, abandoned).
            agent_session_id: Session reference to filter by owning session.
            limit: Maximum results (default: 50).
            project_id: Project ID to filter by. Defaults to current project context.

        Returns:
            Dict with list of worktrees.
        """
        try:
            limit = int(limit) if isinstance(limit, str) else limit
        except ValueError:
            return {"success": False, "error": f"Invalid limit value: {limit!r}"}

        resolved_session_id = agent_session_id
        if agent_session_id:
            try:
                resolved_session_id = ctx.resolve_session_id(agent_session_id)
            except ValueError as e:
                return {"success": False, "error": str(e)}

        worktrees = ctx.worktree_storage.list_worktrees(
            project_id=project_id or ctx.project_id,
            status=status,
            agent_session_id=resolved_session_id,
            limit=limit,
        )

        return {
            "success": True,
            "worktrees": [
                {
                    "id": wt.id,
                    "branch_name": wt.branch_name,
                    "worktree_path": wt.worktree_path,
                    "status": wt.status,
                    "task_id": wt.task_id,
                    "agent_session_id": wt.agent_session_id,
                    "created_at": wt.created_at,
                }
                for wt in worktrees
            ],
            "count": len(worktrees),
        }

    @registry.tool(
        name="get_worktree_stats",
        description="Get worktree statistics for the project.",
    )
    async def get_worktree_stats(project_path: str | None = None) -> dict[str, Any]:
        """Get worktree statistics.

        Args:
            project_path: Path to project directory (pass cwd from CLI).

        Returns:
            Dict with counts by status.
        """
        _, resolved_project_id, error = resolve_project_context(
            project_path, ctx.git_manager, ctx.project_id
        )
        if error:
            return {"success": False, "error": error}

        if resolved_project_id is None:
            return {"success": False, "error": "Project ID unexpectedly None"}

        counts = ctx.worktree_storage.count_by_status(resolved_project_id)

        return {
            "success": True,
            "project_id": resolved_project_id,
            "counts": counts,
            "total": sum(counts.values()),
        }

    @registry.tool(
        name="get_worktree_by_task",
        description="Get worktree linked to a specific task.",
    )
    async def get_worktree_by_task(task_id: str) -> dict[str, Any]:
        """Get worktree linked to a task.

        Args:
            task_id: The task ID to look up.

        Returns:
            Dict with worktree details or not found.
        """
        try:
            resolved_task_id = ctx.resolve_task_id(task_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        worktree = ctx.worktree_storage.get_by_task(resolved_task_id)
        if not worktree:
            return {"success": True, "worktree": None}

        return {
            "success": True,
            "worktree": worktree_dict_with_git_merge_state(worktree, ctx.git_manager),
        }

    return registry
