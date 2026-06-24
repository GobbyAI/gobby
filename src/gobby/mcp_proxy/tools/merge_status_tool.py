"""Registration for the merge_status MCP tool."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_conflict_hydration import normalized_status_conflicts
from gobby.storage.merge_resolutions import MergeResolutionManager
from gobby.worktrees.git import WorktreeGitManager


def register_merge_status_tool(
    registry: InternalToolRegistry,
    *,
    merge_storage: MergeResolutionManager,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
) -> None:
    @registry.tool(
        name="merge_status",
        description=(
            "Get the status of a merge resolution. Conflict file contents are omitted "
            "by default; pass include_content=true only when full content is needed."
        ),
    )
    async def merge_status(resolution_id: str, include_content: bool = False) -> dict[str, Any]:
        """
        Get merge resolution status.

        Args:
            resolution_id: The resolution ID.
            include_content: Include full conflict content fields in the response.

        Returns:
            Dict with resolution details and conflicts.
        """
        if not resolution_id:
            return {"success": False, "error": "resolution_id is required"}

        try:
            resolution = merge_storage.get_resolution(resolution_id)
            if not resolution:
                return {"success": False, "error": f"Resolution '{resolution_id}' not found"}

            (
                conflict_payloads,
                pending_count,
                resolved_count,
                downgraded,
            ) = await normalized_status_conflicts(
                merge_storage=merge_storage,
                worktree_manager=worktree_manager,
                git_manager=git_manager,
                resolution=resolution,
                include_content=include_content,
            )
            if (downgraded or pending_count) and resolution.status == "resolved":
                resolution = (
                    merge_storage.update_resolution(
                        resolution_id=resolution_id,
                        status="pending",
                        force_status=True,
                    )
                    or resolution
                )

            return {
                "success": True,
                "resolution": resolution.to_dict(),
                "conflicts": conflict_payloads,
                "pending_count": pending_count,
                "resolved_count": resolved_count,
            }
        except (OSError, RuntimeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}
