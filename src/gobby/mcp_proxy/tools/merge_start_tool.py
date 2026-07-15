"""Registration for the merge_start MCP tool."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_conflict_hydration import (
    collect_git_conflicts,
    store_missing_conflicts,
)
from gobby.mcp_proxy.tools.merge_direct import _GIT_NO_FF_TIER, _strategy_requests_no_ff
from gobby.mcp_proxy.tools.merge_git_state import (
    resolved_reuse_error,
    source_branch_validation_error,
)
from gobby.mcp_proxy.tools.merge_resolve_locks import (
    release_resolve_lock,
    try_acquire_resolve_lock,
)
from gobby.storage.merge_resolutions import ConflictStatus, MergeResolutionManager
from gobby.worktrees.git import WorktreeGitManager
from gobby.worktrees.merge import MergeResolver, ResolutionTier

logger = logging.getLogger(__name__)


async def _existing_resolution_start_response(
    *,
    merge_storage: MergeResolutionManager,
    git_manager: WorktreeGitManager | None,
    resolution: Any,
    worktree_path: str,
) -> dict[str, Any] | None:
    conflicts = merge_storage.list_conflicts(resolution_id=resolution.id)
    unresolved_conflicts = [
        conflict for conflict in conflicts if conflict.status != ConflictStatus.RESOLVED.value
    ]

    if resolution.status == "resolved":
        stale_reason = await resolved_reuse_error(
            git_manager=git_manager,
            worktree_path=worktree_path,
            target_branch=resolution.target_branch,
        )
        if stale_reason:
            merge_storage.delete_resolution(resolution.id)
            logger.info("Invalidated stale merge resolution %s: %s", resolution.id, stale_reason)
            return None
        return {
            "success": True,
            "resolution_id": resolution.id,
            "tier": resolution.tier_used,
            "needs_human_review": False,
            "conflicts": [],
            "resolved_files": [],
            "reused_resolution": True,
        }

    if resolution.status == "pending" and conflicts:
        return {
            "success": False,
            "resolution_id": resolution.id,
            "tier": resolution.tier_used,
            "needs_human_review": bool(unresolved_conflicts),
            "conflicts": [{"file": conflict.file_path} for conflict in unresolved_conflicts],
            "resolved_files": [],
            "reused_resolution": True,
        }

    return None


def register_merge_start_tool(
    registry: InternalToolRegistry,
    *,
    merge_storage: MergeResolutionManager,
    merge_resolver: MergeResolver,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
) -> None:
    @registry.tool(
        name="merge_start",
        description="Start a merge operation with AI-powered conflict resolution.",
    )
    async def merge_start(
        worktree_id: str,
        source_branch: str,
        target_branch: str | None = None,
        strategy: str = "auto",
    ) -> dict[str, Any]:
        """Start a merge operation."""
        if not worktree_id:
            return {"success": False, "error": "worktree_id is required"}
        if not source_branch:
            return {"success": False, "error": "source_branch is required"}

        worktree_path = None
        worktree_branch = None
        if worktree_manager:
            worktree = worktree_manager.get(worktree_id)
            if worktree and worktree.worktree_path:
                worktree_path = worktree.worktree_path
                branch_value = getattr(worktree, "branch_name", None)
                if isinstance(branch_value, str) and branch_value:
                    worktree_branch = branch_value
                base_branch = getattr(worktree, "base_branch", None)
                if not target_branch and isinstance(base_branch, str) and base_branch:
                    target_branch = base_branch

        if not worktree_path:
            return {
                "success": False,
                "error": f"Worktree '{worktree_id}' not found or has no path",
            }
        target_branch = target_branch or "main"
        validation_error = await source_branch_validation_error(
            git_manager=git_manager,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            source_branch=source_branch,
            target_branch=target_branch,
        )
        if validation_error:
            return {"success": False, "error": validation_error}

        resolution = None
        resolve_lock: asyncio.Lock | None = None
        try:
            existing = merge_storage.get_resolution_for_merge(
                worktree_id=worktree_id,
                source_branch=source_branch,
                target_branch=target_branch,
            )
            no_ff_requested = _strategy_requests_no_ff(strategy)
            if existing:
                existing_response = (
                    None
                    if no_ff_requested and existing.status == "resolved"
                    else await _existing_resolution_start_response(
                        merge_storage=merge_storage,
                        git_manager=git_manager,
                        resolution=existing,
                        worktree_path=worktree_path,
                    )
                )
                if existing_response is not None:
                    return existing_response
                resolution = None if existing.status == "resolved" else existing
            else:
                active = merge_storage.get_active_resolution(worktree_id)
                if active and (
                    active.source_branch != source_branch or active.target_branch != target_branch
                ):
                    return {
                        "success": False,
                        "error": (
                            "Active merge resolution already exists for worktree "
                            f"'{worktree_id}' with source '{active.source_branch}' "
                            f"and target '{active.target_branch}'"
                        ),
                        "resolution_id": active.id,
                    }

                resolution, created = merge_storage.get_or_create_resolution(
                    worktree_id=worktree_id,
                    source_branch=source_branch,
                    target_branch=target_branch,
                    status="pending",
                )
                if not created:
                    existing_response = (
                        None
                        if no_ff_requested and resolution.status == "resolved"
                        else await _existing_resolution_start_response(
                            merge_storage=merge_storage,
                            git_manager=git_manager,
                            resolution=resolution,
                            worktree_path=worktree_path,
                        )
                    )
                    if existing_response is not None:
                        return existing_response
                    if resolution.status == "resolved":
                        resolution, _ = merge_storage.get_or_create_resolution(
                            worktree_id=worktree_id,
                            source_branch=source_branch,
                            target_branch=target_branch,
                            status="pending",
                        )
            if resolution is None:
                resolution, _ = merge_storage.get_or_create_resolution(
                    worktree_id=worktree_id,
                    source_branch=source_branch,
                    target_branch=target_branch,
                    status="pending",
                )

            resolve_lock = await try_acquire_resolve_lock(resolution.id)
            if resolve_lock is None:
                return {
                    "success": False,
                    "error": (
                        "Another merge operation is already running for resolution "
                        f"{resolution.id}. Retry after merge_status."
                    ),
                    "retry_later": True,
                    "resolution_id": resolution.id,
                }

            force_tier = None
            if strategy == "conflict_only":
                force_tier = ResolutionTier.CONFLICT_ONLY_AI
            elif strategy == "full_file":
                force_tier = ResolutionTier.FULL_FILE_AI

            result = await merge_resolver.resolve(
                worktree_path=worktree_path,
                source_branch=source_branch,
                target_branch=target_branch,
                force_tier=force_tier,
            )
            git_conflicts = await collect_git_conflicts(worktree_path, git_manager=git_manager)
            if git_conflicts and (result.success or not result.conflicts):
                result.success = False
                result.conflicts = git_conflicts
                result.unresolved_conflicts = git_conflicts
                result.needs_human_review = True

            tier_used = _GIT_NO_FF_TIER if result.success and no_ff_requested else result.tier.value
            merge_storage.update_resolution(
                resolution_id=resolution.id,
                status="resolved" if result.success else "pending",
                tier_used=tier_used if result.success else None,
            )

            store_missing_conflicts(
                merge_storage,
                resolution.id,
                result.conflicts,
                status="pending" if not result.success else "resolved",
            )

            return {
                "success": result.success,
                "resolution_id": resolution.id,
                "tier": tier_used,
                "needs_human_review": result.needs_human_review,
                "conflicts": [{"file": c.get("file", "")} for c in result.unresolved_conflicts],
                "resolved_files": result.resolved_files,
            }

        except Exception as e:
            logger.exception(
                "Error starting merge for worktree_id=%s, resolution_id=%s",
                worktree_id,
                resolution.id if resolution is not None else "N/A",
            )
            return {"success": False, "error": str(e)}
        finally:
            if resolve_lock is not None and resolution is not None:
                release_resolve_lock(resolution.id, resolve_lock)
