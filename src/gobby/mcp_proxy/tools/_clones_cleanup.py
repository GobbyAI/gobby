"""Stale clone detection and cleanup MCP tools."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.mcp_proxy.tools._clones_context import CloneRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry


def create_clone_cleanup_registry(ctx: CloneRegistryContext) -> InternalToolRegistry:
    """Create a registry with stale clone cleanup tools."""
    registry = InternalToolRegistry(
        name="gobby-clones-cleanup",
        description="Stale clone detection and cleanup tools",
    )

    async def detect_stale_clones(
        hours: int | str = 24,
        limit: int | str = 50,
    ) -> dict[str, Any]:
        """
        Find clones with no activity for a period.

        Args:
            hours: Hours of inactivity threshold (default: 24)
            limit: Maximum results (default: 50)

        Returns:
            Dict with list of stale clones
        """
        hours = int(hours) if isinstance(hours, str) else hours
        limit = int(limit) if isinstance(limit, str) else limit

        stale = ctx.clone_storage.find_stale(
            project_id=ctx.project_id,
            hours=hours,
            limit=limit,
        )

        return {
            "success": True,
            "stale_clones": [
                {
                    "id": c.id,
                    "branch_name": c.branch_name,
                    "clone_path": c.clone_path,
                    "updated_at": c.updated_at,
                    "task_id": c.task_id,
                }
                for c in stale
            ],
            "count": len(stale),
            "threshold_hours": hours,
        }

    registry.register(
        name="detect_stale_clones",
        description="Find clones with no activity for a period",
        input_schema={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Hours of inactivity threshold",
                    "default": 24,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results",
                    "default": 50,
                },
            },
        },
        func=detect_stale_clones,
    )

    async def cleanup_stale_clones(
        hours: int | str = 24,
        dry_run: bool | str = True,
        delete_files: bool | str = False,
    ) -> dict[str, Any]:
        """
        Mark and optionally delete stale clones.

        Args:
            hours: Hours of inactivity threshold (default: 24)
            dry_run: If True, only report what would be cleaned (default: True)
            delete_files: If True, also delete clone files (default: False)

        Returns:
            Dict with cleanup results
        """
        hours = int(hours) if isinstance(hours, str) else hours
        dry_run = dry_run in (True, "true", "True", "1") if isinstance(dry_run, str) else dry_run
        delete_files = (
            delete_files in (True, "true", "True", "1")
            if isinstance(delete_files, str)
            else delete_files
        )

        stale = ctx.clone_storage.cleanup_stale(
            project_id=ctx.project_id,
            hours=hours,
            dry_run=dry_run,
        )

        results = []
        for c in stale:
            result_item: dict[str, Any] = {
                "id": c.id,
                "branch_name": c.branch_name,
                "clone_path": c.clone_path,
                "marked_stale": not dry_run,
                "files_deleted": False,
            }

            if delete_files and not dry_run and ctx.git_manager:
                git_result = await asyncio.to_thread(
                    ctx.git_manager.delete_clone,
                    c.clone_path,
                    force=True,
                )
                result_item["files_deleted"] = git_result.success
                if not git_result.success:
                    result_item["delete_error"] = git_result.error or "Unknown error"

            results.append(result_item)

        return {
            "success": True,
            "dry_run": dry_run,
            "cleaned": results,
            "count": len(results),
            "threshold_hours": hours,
        }

    registry.register(
        name="cleanup_stale_clones",
        description="Mark and optionally delete stale clones",
        input_schema={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Hours of inactivity threshold",
                    "default": 24,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, only report what would be cleaned",
                    "default": True,
                },
                "delete_files": {
                    "type": "boolean",
                    "description": "If true, also delete clone files on disk",
                    "default": False,
                },
            },
        },
        func=cleanup_stale_clones,
    )

    return registry


__all__ = ["create_clone_cleanup_registry"]
