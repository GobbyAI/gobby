"""Clone read, ownership, and association MCP tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools._clones_context import CloneRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.clones import CloneStatus


def create_clone_read_registry(ctx: CloneRegistryContext) -> InternalToolRegistry:
    """Create a registry with clone read and association tools."""
    registry = InternalToolRegistry(
        name="gobby-clones-read",
        description="Clone read, ownership, and association tools",
    )

    def get_clone(clone_id: str) -> dict[str, Any]:
        """
        Get clone by ID.

        Args:
            clone_id: Clone ID

        Returns:
            Dict with clone info or error
        """
        clone = ctx.clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        clone_dict = clone.to_dict()
        clone_dict["disk_exists"] = Path(clone.clone_path).expanduser().is_dir()
        return {"success": True, "clone": clone_dict}

    registry.register(
        name="get_clone",
        description="Get clone by ID",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID",
                },
            },
            "required": ["clone_id"],
        },
        func=get_clone,
    )

    def list_clones(
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        List clones with optional filters.

        Args:
            status: Filter by clone status
            limit: Maximum number of results

        Returns:
            Dict with list of clones
        """
        clones = ctx.clone_storage.list_clones(
            project_id=ctx.project_id,
            status=status,
            limit=limit,
        )

        return {
            "success": True,
            "clones": [c.to_brief() for c in clones],
            "count": len(clones),
        }

    registry.register(
        name="list_clones",
        description="List clones with optional status filter",
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status",
                    "enum": [status.value for status in CloneStatus],
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results",
                    "default": 50,
                },
            },
        },
        func=list_clones,
    )

    def claim_clone(
        clone_id: str,
    ) -> dict[str, Any]:
        """
        Claim a clone for an agent session.

        Args:
            clone_id: Clone ID to claim

        Returns:
            Dict with success status
        """
        from gobby.utils.session_context import get_current_session_id

        session_id = get_current_session_id()
        if not session_id:
            return {"success": False, "error": "No session context available"}
        updated = ctx.clone_storage.claim(clone_id, session_id)
        if not updated:
            clone = ctx.clone_storage.get(clone_id)
            if not clone:
                return {"success": False, "error": f"Clone not found: {clone_id}"}
            owner = f" by session '{clone.agent_session_id}'" if clone.agent_session_id else ""
            return {"success": False, "error": f"Clone already claimed{owner}"}

        return {"success": True}

    registry.register(
        name="claim_clone",
        description="Claim ownership of a clone for an agent session",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID to claim",
                },
            },
            "required": ["clone_id"],
        },
        func=claim_clone,
    )

    def release_clone(clone_id: str) -> dict[str, Any]:
        """
        Release a clone from its current owner.

        Args:
            clone_id: Clone ID to release

        Returns:
            Dict with success status
        """
        clone = ctx.clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        updated = ctx.clone_storage.release(clone_id)
        if not updated:
            return {"success": False, "error": "Failed to release clone"}

        return {"success": True}

    registry.register(
        name="release_clone",
        description="Release ownership of a clone",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID to release",
                },
            },
            "required": ["clone_id"],
        },
        func=release_clone,
    )

    def get_clone_by_task(task_id: str) -> dict[str, Any]:
        """
        Get clone linked to a specific task.

        Args:
            task_id: Task ID to look up

        Returns:
            Dict with clone details or not found
        """
        resolved_task_id = ctx.resolve_task_id(task_id)
        clone = ctx.clone_storage.get_by_task(resolved_task_id)
        if not clone:
            return {"success": True, "clone": None}

        return {"success": True, "clone": clone.to_dict()}

    registry.register(
        name="get_clone_by_task",
        description="Get clone linked to a specific task",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to look up",
                },
            },
            "required": ["task_id"],
        },
        func=get_clone_by_task,
    )

    def link_task_to_clone(
        clone_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """
        Link a task to an existing clone.

        Args:
            clone_id: Clone ID
            task_id: Task ID to link

        Returns:
            Dict with success status
        """
        clone = ctx.clone_storage.get(clone_id)
        if not clone:
            return {"success": False, "error": f"Clone not found: {clone_id}"}

        resolved_task_id = ctx.resolve_task_id(task_id)
        updated = ctx.clone_storage.update(clone_id, task_id=resolved_task_id)
        if not updated:
            return {"success": False, "error": "Failed to link task to clone"}

        return {"success": True}

    registry.register(
        name="link_task_to_clone",
        description="Link a task to an existing clone",
        input_schema={
            "type": "object",
            "properties": {
                "clone_id": {
                    "type": "string",
                    "description": "Clone ID",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID to link",
                },
            },
            "required": ["clone_id", "task_id"],
        },
        func=link_task_to_clone,
    )

    def get_clone_stats() -> dict[str, Any]:
        """
        Get clone statistics for the project.

        Returns:
            Dict with counts by status
        """
        counts = ctx.clone_storage.count_by_status(ctx.project_id)

        return {
            "success": True,
            "project_id": ctx.project_id,
            "counts": counts,
            "total": sum(counts.values()),
        }

    registry.register(
        name="get_clone_stats",
        description="Get clone statistics (counts by status) for the project",
        input_schema={
            "type": "object",
            "properties": {},
        },
        func=get_clone_stats,
    )

    return registry


__all__ = ["create_clone_read_registry"]
