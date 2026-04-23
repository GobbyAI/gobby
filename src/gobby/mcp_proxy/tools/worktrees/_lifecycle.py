"""Worktree lifecycle tools: claim, release, delete, status transitions, link task."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.mcp_proxy.tools.worktrees._helpers import resolve_project_context
from gobby.storage.worktrees import WorktreeStatus

logger = logging.getLogger(__name__)


def create_lifecycle_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with worktree lifecycle tools.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with claim/release/delete/status/link tools
    """
    registry = InternalToolRegistry(
        name="gobby-worktrees-lifecycle",
        description="Worktree lifecycle operations",
    )

    @registry.tool(
        name="claim_worktree",
        description="Claim ownership of a worktree for an agent session. Accepts #N, N, UUID, or prefix for session_id.",
    )
    async def claim_worktree(
        worktree_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Claim a worktree for an agent session.

        Args:
            worktree_id: The worktree ID to claim.
            session_id: Session reference (accepts #N, N, UUID, or prefix) claiming ownership.

        Returns:
            Dict with success status.
        """
        try:
            resolved_session_id = ctx.resolve_session_id(session_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        if worktree.agent_session_id and worktree.agent_session_id != resolved_session_id:
            return {
                "success": False,
                "error": f"Worktree already claimed by session '{worktree.agent_session_id}'",
            }

        updated = ctx.worktree_storage.claim(worktree_id, resolved_session_id)
        if not updated:
            return {"success": False, "error": "Failed to claim worktree"}

        return {"success": True}

    @registry.tool(
        name="release_worktree",
        description="Release ownership of a worktree.",
    )
    async def release_worktree(worktree_id: str) -> dict[str, Any]:
        """Release a worktree from its current owner.

        Args:
            worktree_id: The worktree ID to release.

        Returns:
            Dict with success status.
        """
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        updated = ctx.worktree_storage.release(worktree_id)
        if not updated:
            return {"success": False, "error": "Failed to release worktree"}

        return {"success": True}

    @registry.tool(
        name="delete_worktree",
        description="Delete a worktree (both git and database record).",
    )
    async def delete_worktree(
        worktree_id: str,
        force: bool | str = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Delete a worktree completely (handles all cleanup).

        This is the proper way to remove a worktree. It handles:
        - Removes the worktree directory and all temporary files
        - Cleans up git's worktree tracking (.git/worktrees/)
        - Deletes the associated git branch
        - Removes the Gobby database record

        Do NOT manually run `git worktree remove` - use this tool instead.

        Args:
            worktree_id: The worktree ID to delete (e.g., "wt-abc123").
            force: Force deletion even if there are uncommitted changes.
            project_path: Optional path to project root to resolve git context.

        Returns:
            Dict with success status.
        """
        force = force in (True, "true", "True", "1") if isinstance(force, str) else force

        worktree = ctx.worktree_storage.get(worktree_id)

        if not worktree:
            return {"success": True, "already_deleted": True}

        # Resolve git manager
        resolved_git_mgr = ctx.git_manager
        if project_path:
            try:
                mgr, _, _ = resolve_project_context(project_path, resolved_git_mgr, None)
                if mgr:
                    resolved_git_mgr = mgr
            except (ValueError, OSError) as e:
                logger.debug(
                    f"Failed to resolve project context for project_path={project_path}: {e}"
                )

        worktree_exists = Path(worktree.worktree_path).exists()

        # Check for uncommitted changes if not forcing
        if resolved_git_mgr and worktree_exists:
            status = resolved_git_mgr.get_worktree_status(worktree.worktree_path)
            if status and status.has_uncommitted_changes and not force:
                return {
                    "success": False,
                    "error": "Worktree has uncommitted changes. Use force=True to delete anyway.",
                    "uncommitted_changes": True,
                }

        # Delete git worktree
        if resolved_git_mgr and worktree_exists:
            result = resolved_git_mgr.delete_worktree(
                worktree.worktree_path,
                force=force,
                delete_branch=True,
                branch_name=worktree.branch_name,
            )
            if not result.success:
                return {"success": False, "error": result.error or "Failed to delete git worktree"}
        elif not worktree_exists:
            logger.info(
                f"Worktree path {worktree.worktree_path} doesn't exist, cleaning up DB record only"
            )

        deleted = ctx.worktree_storage.delete(worktree_id)
        if not deleted:
            return {"success": False, "error": "Failed to delete worktree record"}

        return {"success": True}

    @registry.tool(
        name="mark_worktree_merged",
        description="Mark a worktree as merged (ready for cleanup).",
    )
    async def mark_worktree_merged(worktree_id: str) -> dict[str, Any]:
        """Mark a worktree as merged.

        Args:
            worktree_id: The worktree ID to mark.

        Returns:
            Dict with success status.
        """
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        updated = ctx.worktree_storage.mark_merged(worktree_id)
        if not updated:
            return {"success": False, "error": "Failed to mark worktree as merged"}

        return {"success": True}

    @registry.tool(
        name="abandon_worktree",
        description="Mark a worktree as abandoned.",
    )
    async def abandon_worktree(worktree_id: str) -> dict[str, Any]:
        """Mark a worktree as abandoned.

        Args:
            worktree_id: The worktree ID to abandon.

        Returns:
            Dict with success status.
        """
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        updated = ctx.worktree_storage.mark_abandoned(worktree_id)
        if not updated:
            return {"success": False, "error": "Failed to abandon worktree"}

        return {"success": True}

    @registry.tool(
        name="reactivate_worktree",
        description="Reactivate a worktree without merging or deleting it.",
    )
    async def reactivate_worktree(worktree_id: str) -> dict[str, Any]:
        """Reactivate a worktree.

        Args:
            worktree_id: The worktree ID to reactivate.

        Returns:
            Dict with success status.
        """
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        updated = ctx.worktree_storage.update(
            worktree_id,
            status=WorktreeStatus.ACTIVE.value,
            merged_at=None,
            cleanup_after=None,
        )
        if not updated:
            return {"success": False, "error": "Failed to reactivate worktree"}

        return {"success": True}

    @registry.tool(
        name="link_task_to_worktree",
        description="Link a task to an existing worktree.",
    )
    async def link_task_to_worktree(
        worktree_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Link a task to a worktree.

        Args:
            worktree_id: The worktree ID.
            task_id: The task ID to link.

        Returns:
            Dict with success status.
        """
        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        try:
            resolved_task_id = ctx.resolve_task_id(task_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        updated = ctx.worktree_storage.update(worktree_id, task_id=resolved_task_id)
        if not updated:
            return {"success": False, "error": "Failed to link task to worktree"}

        return {"success": True}

    return registry
