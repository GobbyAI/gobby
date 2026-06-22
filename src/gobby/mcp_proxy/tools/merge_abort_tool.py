"""Registration for the merge_abort MCP tool."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_git_state import merge_head_exists
from gobby.mcp_proxy.tools.merge_github_protection import git_output
from gobby.storage.merge_resolutions import MergeResolutionManager
from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger(__name__)


def register_merge_abort_tool(
    registry: InternalToolRegistry,
    *,
    merge_storage: MergeResolutionManager,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
) -> None:
    @registry.tool(
        name="merge_abort",
        description="Abort the merge operation and restore the previous state.",
    )
    async def merge_abort(resolution_id: str) -> dict[str, Any]:
        """
        Abort a merge operation.

        Args:
            resolution_id: The resolution ID.

        Returns:
            Dict with abort status.
        """
        if not resolution_id:
            return {"success": False, "error": "resolution_id is required"}

        resolution = merge_storage.get_resolution(resolution_id)
        if not resolution:
            return {"success": False, "error": f"Resolution '{resolution_id}' not found"}

        if resolution.status == "resolved":
            return {"success": False, "error": "Cannot abort: merge is already resolved"}

        try:
            if not git_manager or not worktree_manager:
                return {
                    "success": False,
                    "error": "git_manager or worktree_manager not configured",
                }

            worktree = worktree_manager.get(resolution.worktree_id)
            if not worktree or not worktree.worktree_path:
                return {
                    "success": False,
                    "error": f"Worktree '{resolution.worktree_id}' not found or has no path",
                }

            wt_path = worktree.worktree_path
            if await merge_head_exists(git_manager, wt_path):
                abort_result = await asyncio.to_thread(
                    git_manager.run_git_command,
                    ["merge", "--abort"],
                    cwd=wt_path,
                    timeout=30,
                )
                if abort_result.returncode != 0:
                    return {
                        "success": False,
                        "error": f"git merge --abort failed: {git_output(abort_result)}",
                        "resolution_id": resolution_id,
                    }

            deleted = merge_storage.delete_resolution(resolution_id)

            if deleted:
                return {
                    "success": True,
                    "message": "Merge aborted successfully",
                    "resolution_id": resolution_id,
                }
            return {"success": False, "error": "Failed to abort merge"}

        except Exception as e:
            logger.exception("Error aborting merge for resolution_id=%s", resolution_id)
            return {"success": False, "error": str(e)}
