"""
Task sync and commit linking MCP tools module.

Provides tools for task synchronization and commit linking:
- sync_tasks: Manually trigger task import/export
- get_sync_status: Get current sync status
- link_commit: Link a git commit to a task
- unlink_commit: Unlink a git commit from a task
- auto_link_commits: Auto-detect and link commits mentioning task IDs
- get_task_diff: Get combined diff for all commits linked to a task

Extracted from tasks.py using Strangler Fig pattern for code decomposition.
"""

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.tasks import TaskNotFoundError
from gobby.utils.project_context import get_project_context

if TYPE_CHECKING:
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.sync.tasks import TaskSyncManager

__all__ = ["create_sync_registry"]


def get_current_project_id() -> str | None:
    """Get the current project ID from context."""
    context = get_project_context()
    return context.get("id") if context else None


def create_sync_registry(
    sync_manager: "TaskSyncManager | None" = None,
    task_manager: "LocalTaskManager | None" = None,
    project_manager: "LocalProjectManager | None" = None,
    auto_link_commits_fn: Callable[..., Any] | None = None,
    get_task_diff_fn: Callable[..., Any] | None = None,
    session_manager: Any | None = None,
) -> InternalToolRegistry:
    """
    Create a registry with task sync and commit linking tools.

    Args:
        sync_manager: TaskSyncManager instance
        task_manager: LocalTaskManager instance (required for task ID resolution)
        project_manager: LocalProjectManager instance (for repo_path lookup)
        auto_link_commits_fn: Function for auto-linking commits (injectable for testing)
        get_task_diff_fn: Function for getting task diff (injectable for testing)

    Returns:
        InternalToolRegistry with sync tools registered
    """
    # Lazy import to avoid circular dependency
    from gobby.mcp_proxy.tools.tasks import resolve_task_id_for_mcp

    registry = InternalToolRegistry(
        name="gobby-tasks-sync",
        description="Task synchronization and commit linking tools",
    )

    if sync_manager is None:
        raise ValueError("sync_manager is required")
    if task_manager is None:
        raise ValueError("task_manager is required for task ID resolution")

    # --- sync_tasks ---

    def sync_tasks(direction: str = "both", project: str | None = None) -> dict[str, Any]:
        """Manually trigger task synchronization."""
        valid_directions = ("import", "export", "both")
        if direction not in valid_directions:
            return {
                "error": f"Invalid direction '{direction}'. Must be one of: {', '.join(valid_directions)}"
            }

        try:
            from gobby.mcp_proxy.tools.tasks._context import resolve_project_filter_standalone

            project_id = resolve_project_filter_standalone(project, False, task_manager.db)
        except ValueError as e:
            return {"error": str(e)}

        result = {}
        if direction in ["import", "both"]:
            sync_manager.import_from_jsonl(project_id=project_id)
            result["import"] = "completed"

        if direction in ["export", "both"]:
            sync_manager.export_to_jsonl(project_id=project_id)
            result["export"] = "completed"

        return result

    registry.register(
        name="sync_tasks",
        description="Manually trigger task synchronization.",
        input_schema={
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "description": '"import", "export", or "both"',
                    "default": "both",
                },
                "project": {
                    "type": "string",
                    "description": "Project name or UUID to scope sync",
                },
            },
        },
        func=sync_tasks,
    )

    # --- get_sync_status ---

    def get_sync_status() -> dict[str, Any]:
        """Get current synchronization status."""
        result: dict[str, Any] = sync_manager.get_sync_status()
        return result

    registry.register(
        name="get_sync_status",
        description="Get current synchronization status.",
        input_schema={"type": "object", "properties": {}},
        func=get_sync_status,
    )

    # --- link_commit ---

    def link_commit(task_id: str, commit_sha: str) -> dict[str, Any]:
        """Link a git commit to a task."""
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        # Get project repo path for git operations
        ctx = get_project_context()
        repo_path = None
        if ctx and ctx.get("id") and project_manager:
            project = project_manager.get(ctx["id"])
            if project:
                repo_path = project.repo_path

        try:
            task = task_manager.link_commit(resolved_task_id, commit_sha, cwd=repo_path)
            return {
                "task_id": task.id,
                "commits": task.commits or [],
            }
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="link_commit",
        description="Link a git commit to a task. NOTE: For closing tasks, prefer close_task(task_id, commit_sha='...') which links and closes in one call. Use link_commit only when you need to link without closing.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, N (seq_num), path (1.2.3), or UUID",
                },
                "commit_sha": {
                    "type": "string",
                    "description": "Git commit SHA (short or full)",
                },
            },
            "required": ["task_id", "commit_sha"],
        },
        func=link_commit,
    )

    # --- unlink_commit ---

    def unlink_commit(task_id: str, commit_sha: str) -> dict[str, Any]:
        """Unlink a git commit from a task."""
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        # Get project repo path for git operations
        ctx = get_project_context()
        repo_path = None
        if ctx and ctx.get("id") and project_manager:
            project = project_manager.get(ctx["id"])
            if project:
                repo_path = project.repo_path

        try:
            task = task_manager.unlink_commit(resolved_task_id, commit_sha, cwd=repo_path)
            return {
                "task_id": task.id,
                "commits": task.commits or [],
            }
        except ValueError as e:
            return {"error": str(e)}

    registry.register(
        name="unlink_commit",
        description="Unlink a git commit from a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, N (seq_num), path (1.2.3), or UUID",
                },
                "commit_sha": {
                    "type": "string",
                    "description": "Git commit SHA to unlink",
                },
            },
            "required": ["task_id", "commit_sha"],
        },
        func=unlink_commit,
    )

    # --- auto_link_commits ---

    def auto_link_commits(
        task_id: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Auto-detect and link commits that mention task IDs."""
        if auto_link_commits_fn is None:
            return {"error": "auto_link_commits_fn not configured"}

        # Validate task exists if provided, but keep original #N format
        # because extract_task_ids_from_message returns #N format from git log
        if task_id:
            try:
                resolve_task_id_for_mcp(task_manager, task_id)
            except (TaskNotFoundError, ValueError) as e:
                return {"error": f"Invalid task_id: {e}"}

        # Get project repo_path
        ctx = get_project_context()
        repo_path = None
        if ctx and ctx.get("id") and project_manager:
            project = project_manager.get(ctx["id"])
            if project:
                repo_path = project.repo_path

        # Get project_id for resolving #N task references
        project_id = ctx.get("id") if ctx else None

        result = auto_link_commits_fn(
            task_manager=task_manager,
            task_id=task_id,  # Pass original #N format, not UUID
            since=since,
            cwd=repo_path,
            project_id=project_id,
        )

        return {
            "linked_tasks": result.linked_tasks,
            "total_linked": result.total_linked,
            "skipped": result.skipped,
        }

    registry.register(
        name="auto_link_commits",
        description="Auto-detect and link commits that mention task IDs in their messages. "
        "Supports patterns: [gt-xxxxx], gt-xxxxx:, Implements/Fixes/Closes gt-xxxxx.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Filter to specific task (#N, N, path, or UUID). Optional.",
                    "default": None,
                },
                "since": {
                    "type": "string",
                    "description": "Git --since parameter (e.g., '1 week ago', '2024-01-01')",
                    "default": None,
                },
            },
        },
        func=auto_link_commits,
    )

    # --- get_task_diff ---

    def get_task_diff_tool(
        task_id: str,
        include_uncommitted: bool = False,
    ) -> dict[str, Any]:
        """Get the combined diff for all commits linked to a task."""
        # Resolve task reference
        try:
            resolved_task_id = resolve_task_id_for_mcp(task_manager, task_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Invalid task_id: {e}"}

        task = task_manager.get_task(resolved_task_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        if get_task_diff_fn is None:
            return {"error": "get_task_diff_fn not configured"}

        # Get project repo_path
        repo_path = None
        if project_manager and task.project_id:
            project = project_manager.get(task.project_id)
            if project:
                repo_path = project.repo_path

        result = get_task_diff_fn(
            task_id=resolved_task_id,
            task_manager=task_manager,
            include_uncommitted=include_uncommitted,
            cwd=repo_path,
        )

        return {
            "diff": result.diff,
            "commits": result.commits,
            "has_uncommitted_changes": result.has_uncommitted_changes,
            "file_count": result.file_count,
        }

    registry.register(
        name="get_task_diff",
        description="Get the combined diff for all commits linked to a task. "
        "Optionally include uncommitted changes.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task reference: #N, N (seq_num), path (1.2.3), or UUID",
                },
                "include_uncommitted": {
                    "type": "boolean",
                    "description": "Include uncommitted changes in the diff",
                    "default": False,
                },
            },
            "required": ["task_id"],
        },
        func=get_task_diff_tool,
    )

    # ─── Async sync tools (thin wrappers around workflow actions) ───

    @registry.tool(
        name="sync_import",
        description="Import tasks from .gobby/tasks.jsonl into the database.",
    )
    async def sync_import_tool(session_id: str) -> dict[str, Any]:
        """
        Import tasks from JSONL file using session for project resolution.

        Args:
            session_id: Session ID for project-scoped sync
        """
        if not session_manager:
            return {"success": False, "error": "Session manager not available"}
        try:
            project_id = None
            session = await asyncio.to_thread(session_manager.get, session_id)
            if session:
                project_id = session.project_id
            await asyncio.to_thread(sync_manager.import_from_jsonl, project_id=project_id)
            return {"success": True, "imported": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="sync_export",
        description="Export tasks from the database to .gobby/tasks.jsonl.",
    )
    async def sync_export_tool(session_id: str) -> dict[str, Any]:
        """
        Export tasks to JSONL file using session for project resolution.

        Args:
            session_id: Session ID for project-scoped sync
        """
        if not session_manager:
            return {"success": False, "error": "Session manager not available"}
        try:
            project_id = None
            session = await asyncio.to_thread(session_manager.get, session_id)
            if session:
                project_id = session.project_id
            await asyncio.to_thread(sync_manager.export_to_jsonl, project_id=project_id)
            return {"success": True, "exported": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return registry
