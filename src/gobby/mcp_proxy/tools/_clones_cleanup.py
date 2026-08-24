"""Stale clone detection and cleanup MCP tools."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.mcp_proxy.tools._clones_context import CloneRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.clones import CloneStatus
from gobby.utils.git import run_to_completion


def create_clone_cleanup_registry(ctx: CloneRegistryContext) -> InternalToolRegistry:
    """Create a registry with stale clone cleanup tools."""
    registry = InternalToolRegistry(
        name="gobby-clones-cleanup",
        description="Stale clone detection and cleanup tools",
    )

    def detect_stale_clones(
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

    async def _cleanup_stale_clones_impl(
        hours: int | str = 24,
        dry_run: bool | str = True,
        delete_files: bool | str = False,
        cancellation_requested: asyncio.Event | None = None,
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
        cleanup_succeeded = True
        for c in stale:
            if cancellation_requested is not None and cancellation_requested.is_set():
                raise asyncio.CancelledError
            result_item: dict[str, Any] = {
                "id": c.id,
                "branch_name": c.branch_name,
                "clone_path": c.clone_path,
                "marked_stale": not dry_run,
                "files_deleted": False,
                "record_deleted": False,
            }

            if delete_files and not dry_run and ctx.git_manager:
                try:
                    terminal = ctx.clone_storage.mark_cleanup(c.id)
                except Exception as error:
                    terminal = None
                    result_item["record_terminal_error"] = str(error)
                if terminal is None:
                    cleanup_succeeded = False
                    result_item.setdefault(
                        "record_terminal_error",
                        "Clone record disappeared before cleanup could mark it terminal",
                    )
                    results.append(result_item)
                    continue

                result_item["record_terminal"] = True
                delete_error: str | None
                try:
                    git_result = await asyncio.to_thread(
                        ctx.git_manager.delete_clone,
                        c.clone_path,
                        force=True,
                    )
                except Exception as error:
                    delete_error = str(error)
                    result_item["files_deleted"] = False
                else:
                    delete_error = (
                        None if git_result.success else git_result.error or "Unknown error"
                    )
                    result_item["files_deleted"] = git_result.success

                if delete_error is None:
                    try:
                        deleted = ctx.clone_storage.delete(c.id)
                    except Exception as error:
                        result_item["record_delete_error"] = str(error)
                        cleanup_succeeded = False
                    else:
                        # False means the row was already absent, which is also a
                        # consistent terminal state after file removal.
                        result_item["record_deleted"] = True
                        if not deleted:
                            result_item["record_already_missing"] = True
                else:
                    result_item["delete_error"] = delete_error
                    cleanup_succeeded = False
                    try:
                        restored = ctx.clone_storage.update(
                            c.id,
                            status=CloneStatus.STALE.value,
                        )
                    except Exception as error:
                        result_item["record_restore_error"] = str(error)
                    else:
                        result_item["record_terminal"] = False
                        result_item["record_restored"] = restored is not None

            results.append(result_item)

        return {
            "success": cleanup_succeeded,
            "dry_run": dry_run,
            "cleaned": results,
            "count": len(results),
            "threshold_hours": hours,
        }

    async def cleanup_stale_clones(
        hours: int | str = 24,
        dry_run: bool | str = True,
        delete_files: bool | str = False,
    ) -> dict[str, Any]:
        cancellation_requested = asyncio.Event()
        return await run_to_completion(
            _cleanup_stale_clones_impl(
                hours,
                dry_run,
                delete_files,
                cancellation_requested,
            ),
            on_cancel=cancellation_requested.set,
        )

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
