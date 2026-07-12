"""Registration for the merge_resolve MCP tool."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_conflict_hydration import conflict_hunks_for_ai
from gobby.mcp_proxy.tools.merge_resolve_locks import try_acquire_resolve_lock
from gobby.storage.merge_resolutions import ConflictStatus, MergeResolutionManager
from gobby.worktrees.merge import MergeResolver
from gobby.worktrees.merge.resolver import assert_marker_free

logger = logging.getLogger(__name__)


def register_merge_resolve_tool(
    registry: InternalToolRegistry,
    *,
    merge_storage: MergeResolutionManager,
    merge_resolver: MergeResolver,
    worktree_manager: Any | None = None,
) -> None:
    @registry.tool(
        name="merge_resolve",
        description="Resolve a specific conflict, optionally with AI assistance.",
    )
    async def merge_resolve(
        conflict_id: str,
        resolved_content: str | None = None,
        use_ai: bool = True,
    ) -> dict[str, Any]:
        """
        Resolve a specific conflict.

        Args:
            conflict_id: The conflict ID.
            resolved_content: Manual resolution content (skips AI).
            use_ai: Whether to use AI for resolution (default: True).

        Returns:
            Dict with resolution result.
        """
        if not conflict_id:
            return {"success": False, "error": "conflict_id is required"}

        conflict = merge_storage.get_conflict(conflict_id)
        if not conflict:
            return {"success": False, "error": f"Conflict '{conflict_id}' not found"}

        resolve_lock: asyncio.Lock | None = None
        try:
            if resolved_content is not None:
                try:
                    assert_marker_free(resolved_content)
                except ValueError as exc:
                    return {"success": False, "error": str(exc)}
                updated = merge_storage.update_conflict(
                    conflict_id=conflict_id,
                    status=ConflictStatus.RESOLVED.value,
                    resolved_content=resolved_content,
                )
                return {
                    "success": True,
                    "conflict": updated.to_dict() if updated else None,
                    "resolution_method": "manual",
                }

            if use_ai:
                resolve_lock = await try_acquire_resolve_lock(conflict.resolution_id)
                if resolve_lock is None:
                    return {
                        "success": False,
                        "error": (
                            "Another merge_resolve call is already running for "
                            f"resolution {conflict.resolution_id}. Retry sequentially "
                            "after merge_status; do not parallelize conflicts from the "
                            "same active resolution."
                        ),
                        "retry_later": True,
                        "resolution_id": conflict.resolution_id,
                    }
                worktree_path = None
                resolution = merge_storage.get_resolution(conflict.resolution_id)
                if resolution and worktree_manager:
                    worktree = worktree_manager.get(resolution.worktree_id)
                    if worktree and worktree.worktree_path:
                        worktree_path = worktree.worktree_path
                result = await merge_resolver.resolve_file(
                    path=conflict.file_path,
                    conflict_hunks=await conflict_hunks_for_ai(conflict, worktree_path),
                    worktree_path=worktree_path,
                )

                if result.success:
                    resolved = result.resolved_content_by_file.get(conflict.file_path)
                    if not resolved:
                        return {
                            "success": False,
                            "error": (
                                "AI resolver returned success but produced no content "
                                f"for {conflict.file_path}"
                            ),
                            "needs_human_review": True,
                        }
                    updated = merge_storage.update_conflict(
                        conflict_id=conflict_id,
                        status=ConflictStatus.RESOLVED.value,
                        resolved_content=resolved,
                    )
                    return {
                        "success": True,
                        "conflict": updated.to_dict() if updated else None,
                        "resolution_method": "ai",
                        "tier": result.tier.value,
                    }
                return {
                    "success": False,
                    "error": "AI resolution failed",
                    "needs_human_review": result.needs_human_review,
                    "failure_reason": result.failure_reason,
                }

            return {"success": False, "error": "No resolution method specified"}

        except Exception as e:
            logger.exception("Error resolving conflict %s", conflict_id)
            return {"success": False, "error": str(e)}
        finally:
            if resolve_lock is not None and resolve_lock.locked():
                resolve_lock.release()
