"""Search operations for task management.

Provides FTS5 full-text search tools for tasks.
"""

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp


def create_search_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with task search tools.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with search tools registered
    """
    registry = InternalToolRegistry(
        name="gobby-tasks-search",
        description="Task search operations",
    )

    def search_tasks(
        query: str,
        current_stage_state: str | list[str] | None = None,
        task_type: str | None = None,
        priority: int | None = None,
        parent_task_id: str | None = None,
        category: str | None = None,
        limit: int = 20,
        min_score: float = 0.0,
        all_projects: bool = False,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Search tasks using FTS5 full-text search.

        Performs keyword-based full-text search (FTS5) on task title, description, labels, and type.
        Results are ranked by relevance and can be filtered by current stage state, type, etc.

        Args:
            query: Search query text (required). Natural language query.
            current_stage_state: Filter by current stage state.
                Can be a single state or comma-separated list.
            task_type: Filter by task type (task, bug, feature, epic)
            priority: Filter by priority (1=High, 2=Medium, 3=Low)
            parent_task_id: Filter by parent task ID (UUID, #N, or N format)
            category: Filter by task category
            limit: Maximum number of results (default: 20)
            min_score: Minimum similarity score 0.0-1.0 (default: 0.0)
            all_projects: If true, search all projects instead of current project
            project: Filter by project name or UUID (optional)

        Returns:
            Dict with matching tasks and their similarity scores
        """
        if not query or not query.strip():
            return {"error": "Query is required", "tasks": [], "count": 0}

        try:
            project_id = ctx.resolve_project_filter(project, all_projects)
        except ValueError as e:
            return {"error": str(e), "tasks": [], "count": 0}

        # Handle comma-separated current-stage state string.
        current_stage_filter: str | list[str] | None = current_stage_state
        if isinstance(current_stage_state, str) and "," in current_stage_state:
            current_stage_filter = [s.strip() for s in current_stage_state.split(",")]

        # Resolve parent_task_id if provided (#N, N, or UUID -> UUID)
        resolved_parent_id = None
        if parent_task_id:
            try:
                resolved_parent_id = resolve_task_id_for_mcp(
                    ctx.task_manager, parent_task_id, project_id
                )
            except Exception:
                return {
                    "error": f"Invalid parent_task_id: {parent_task_id}",
                    "tasks": [],
                    "count": 0,
                }

        # Perform search
        results = ctx.task_manager.search_tasks(
            query=query.strip(),
            project_id=project_id,
            current_stage_state=current_stage_filter,
            task_type=task_type,
            priority=priority,
            parent_task_id=resolved_parent_id,
            category=category,
            limit=limit,
            min_score=min_score,
        )

        return {
            "tasks": [
                {
                    **task.to_brief(),
                    "score": round(score, 4),
                }
                for task, score in results
            ],
            "count": len(results),
            "query": query.strip(),
        }

    registry.register(
        name="search_tasks",
        description="Search tasks using FTS5 full-text search. Returns tasks ranked by relevance to the query.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query text. Natural language query to find matching tasks.",
                },
                "current_stage_state": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Filter by current stage state. Can be a single state or comma-separated list.",
                    "default": None,
                },
                "task_type": {
                    "type": "string",
                    "description": "Filter by task type (task, bug, feature, epic)",
                    "default": None,
                },
                "priority": {
                    "type": "integer",
                    "description": "Filter by priority (1=High, 2=Medium, 3=Low)",
                    "default": None,
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Filter by parent task ID (UUID, #N, or N format)",
                    "default": None,
                },
                "category": {
                    "type": "string",
                    "description": "Filter by task category",
                    "default": None,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return",
                    "default": 20,
                },
                "min_score": {
                    "type": "number",
                    "description": "Minimum similarity score threshold (0.0-1.0)",
                    "default": 0.0,
                },
                "all_projects": {
                    "type": "boolean",
                    "description": "If true, search all projects instead of just the current project",
                    "default": False,
                },
                "project": {
                    "type": "string",
                    "description": "Filter by project name or UUID (e.g., '_personal')",
                    "default": None,
                },
            },
            "required": ["query"],
        },
        func=search_tasks,
    )

    return registry


def create_reindex_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with the reindex_tasks tool (gobby-tasks-reindex).

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with reindex tool registered
    """
    registry = InternalToolRegistry(
        name="gobby-tasks-reindex",
        description="Task search index admin operations",
    )

    def reindex_tasks(all_projects: bool = False, project: str | None = None) -> dict[str, Any]:
        """Force rebuild of the task search index.

        Use this to refresh the search index after bulk operations
        or if search results seem stale.

        Args:
            all_projects: If true, reindex all projects instead of current project
            project: Filter by project name or UUID (optional)

        Returns:
            Dict with index statistics
        """
        try:
            project_id = ctx.resolve_project_filter(project, all_projects)
        except ValueError as e:
            return {"error": str(e)}

        stats = ctx.task_manager.reindex_search(project_id)

        return {
            "message": f"Search index rebuilt with {stats.get('item_count', 0)} tasks",
            "stats": stats,
        }

    registry.register(
        name="reindex_tasks",
        description="Force rebuild of the task search index. Use after bulk operations or if search seems stale.",
        input_schema={
            "type": "object",
            "properties": {
                "all_projects": {
                    "type": "boolean",
                    "description": "If true, reindex all projects instead of just the current project",
                    "default": False,
                },
                "project": {
                    "type": "string",
                    "description": "Filter by project name or UUID (e.g., '_personal')",
                    "default": None,
                },
            },
        },
        func=reindex_tasks,
    )

    return registry
