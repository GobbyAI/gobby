"""Worktree cleanup and stale detection tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.mcp_proxy.tools.worktrees._helpers import resolve_project_context
from gobby.mcp_proxy.tools.worktrees._merge_state import is_worktree_git_merged

logger = logging.getLogger(__name__)


def create_cleanup_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with worktree cleanup tools.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with stale detection and cleanup tools
    """
    registry = InternalToolRegistry(
        name="gobby-worktrees-cleanup",
        description="Worktree stale detection and cleanup",
    )

    @registry.tool(
        name="detect_stale_worktrees",
        description="Find worktrees with no activity for a period.",
    )
    def detect_stale_worktrees(
        project_path: str | None = None,
        hours: int | str = 24,
        limit: int | str = 50,
    ) -> dict[str, Any]:
        """Find stale worktrees (no activity for N hours).

        Args:
            project_path: Path to project directory (pass cwd from CLI).
            hours: Hours of inactivity threshold (default: 24).
            limit: Maximum results (default: 50).

        Returns:
            Dict with list of stale worktrees.
        """
        try:
            hours = int(hours) if isinstance(hours, str) else hours
        except ValueError:
            return {"success": False, "error": f"Invalid value for hours: {hours!r}"}
        try:
            limit = int(limit) if isinstance(limit, str) else limit
        except ValueError:
            return {"success": False, "error": f"Invalid value for limit: {limit!r}"}

        _, resolved_project_id, error = resolve_project_context(
            project_path, ctx.git_manager, ctx.project_id
        )
        if error:
            return {"success": False, "error": error}
        if resolved_project_id is None:
            return {"success": False, "error": "Could not resolve project ID"}

        stale = ctx.worktree_storage.find_stale(
            project_id=resolved_project_id,
            hours=hours,
            limit=limit,
        )

        return {
            "success": True,
            "stale_worktrees": [
                {
                    "id": wt.id,
                    "branch_name": wt.branch_name,
                    "worktree_path": wt.worktree_path,
                    "updated_at": wt.updated_at,
                    "task_id": wt.task_id,
                }
                for wt in stale
            ],
            "count": len(stale),
            "threshold_hours": hours,
        }

    @registry.tool(
        name="cleanup_stale_worktrees",
        description="Mark and optionally delete stale worktrees.",
    )
    async def cleanup_stale_worktrees(
        project_path: str | None = None,
        hours: int | str = 24,
        dry_run: bool | str = True,
        delete_git: bool | str = False,
        force_delete_branch: bool | str = False,
    ) -> dict[str, Any]:
        """Cleanup stale worktrees.

        Args:
            project_path: Path to project directory (pass cwd from CLI).
            hours: Hours of inactivity threshold (default: 24).
            dry_run: If True, only report what would be cleaned (default: True).
            delete_git: If True, also delete git worktrees (default: False).
            force_delete_branch: Force-delete unmerged branches (default: False).

        Returns:
            Dict with cleanup results.
        """

        def _parse_bool(value: bool | str) -> bool:
            if isinstance(value, bool):
                return value
            return value.lower() in ("true", "1", "yes", "on")

        hours = int(hours) if isinstance(hours, str) else hours
        dry_run = _parse_bool(dry_run)
        delete_git = _parse_bool(delete_git)
        force_delete_branch = _parse_bool(force_delete_branch)

        resolved_git_manager, resolved_project_id, error = resolve_project_context(
            project_path, ctx.git_manager, ctx.project_id
        )
        if error:
            return {"success": False, "error": error}
        if resolved_project_id is None:
            return {"success": False, "error": "Could not resolve project ID"}

        stale = ctx.worktree_storage.cleanup_stale(
            project_id=resolved_project_id,
            hours=hours,
            dry_run=dry_run,
        )

        # Also include expired merged worktrees (past cleanup_after window)
        expired = ctx.worktree_storage.find_expired(project_id=resolved_project_id)

        results = []
        for wt in stale:
            result: dict[str, Any] = {
                "id": wt.id,
                "branch_name": wt.branch_name,
                "worktree_path": wt.worktree_path,
                "reason": "stale",
                "marked_abandoned": not dry_run,
                "git_deleted": False,
            }

            if delete_git and not dry_run and resolved_git_manager:
                status = await asyncio.to_thread(
                    resolved_git_manager.get_worktree_status, wt.worktree_path
                )
                if status is None:
                    result["git_skipped"] = True
                    result["git_skip_reason"] = "Could not verify worktree status"
                    results.append(result)
                    continue
                if status.has_uncommitted_changes:
                    result["git_skipped"] = True
                    result["git_skip_reason"] = "Worktree has uncommitted changes"
                    results.append(result)
                    continue
                git_result = await asyncio.to_thread(
                    resolved_git_manager.delete_worktree,
                    wt.worktree_path,
                    force=False,
                    delete_branch=True,
                    force_delete_branch=force_delete_branch,
                    branch_name=wt.branch_name,
                    base_branch=wt.base_branch,
                )
                result["git_deleted"] = git_result.success
                if not git_result.success:
                    result["git_error"] = git_result.error or "Unknown error"

            results.append(result)

        # Expired merged worktrees: always delete git (work is in target branch)
        for wt in expired:
            result = {
                "id": wt.id,
                "branch_name": wt.branch_name,
                "worktree_path": wt.worktree_path,
                "reason": "expired",
                "cleanup_after": wt.cleanup_after,
                "marked_abandoned": False,
                "git_deleted": False,
            }

            if not dry_run and resolved_git_manager:
                git_merged = await asyncio.to_thread(
                    is_worktree_git_merged, wt, resolved_git_manager
                )
                if git_merged is not True and not force_delete_branch:
                    result["git_skipped"] = True
                    result["git_skip_reason"] = "Git no longer reports the branch as merged"
                    results.append(result)
                    continue
                status = await asyncio.to_thread(
                    resolved_git_manager.get_worktree_status, wt.worktree_path
                )
                if status is not None and status.has_uncommitted_changes:
                    result["git_skipped"] = True
                    result["git_skip_reason"] = "Worktree has uncommitted changes"
                    results.append(result)
                    continue
                git_result = await asyncio.to_thread(
                    resolved_git_manager.delete_worktree,
                    wt.worktree_path,
                    force=False,
                    delete_branch=True,
                    force_delete_branch=force_delete_branch,
                    branch_name=wt.branch_name,
                    base_branch=wt.base_branch,
                )
                result["git_deleted"] = git_result.success
                if git_result.success:
                    ctx.worktree_storage.delete(wt.id)
                else:
                    result["git_error"] = git_result.error or "Unknown error"

            results.append(result)

        return {
            "success": True,
            "dry_run": dry_run,
            "cleaned": results,
            "count": len(results),
            "threshold_hours": hours,
        }

    return registry
