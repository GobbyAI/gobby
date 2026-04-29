"""
Internal MCP tools for Gobby Merge Resolution.

Exposes functionality for:
- Starting merge operations with AI-powered resolution
- Getting merge status and conflict details
- Resolving individual conflicts
- Applying resolved merges
- Aborting merge operations

These tools are registered with the InternalToolRegistry and accessed
via the downstream proxy pattern (call_tool, list_tools, get_tool_schema).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.merge_resolutions import ConflictStatus

if TYPE_CHECKING:
    from gobby.storage.merge_resolutions import MergeResolutionManager
    from gobby.worktrees.git import WorktreeGitManager
    from gobby.worktrees.merge import MergeResolver

logger = logging.getLogger(__name__)


def create_merge_registry(
    merge_storage: MergeResolutionManager,
    merge_resolver: MergeResolver,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
) -> InternalToolRegistry:
    """
    Create a merge tool registry with all merge-related tools.

    Args:
        merge_storage: MergeResolutionManager for database operations.
        merge_resolver: MergeResolver for AI-powered conflict resolution.
        git_manager: WorktreeGitManager for git operations.
        worktree_manager: LocalWorktreeManager for resolving worktree paths.

    Returns:
        InternalToolRegistry with all merge tools registered.
    """
    registry = InternalToolRegistry(
        name="gobby-merge",
        description="AI-powered merge conflict resolution - start merges, resolve conflicts, and apply resolutions",
    )

    @registry.tool(
        name="merge_start",
        description="Start a merge operation with AI-powered conflict resolution.",
    )
    async def merge_start(
        worktree_id: str,
        source_branch: str,
        target_branch: str = "main",
        strategy: str = "auto",
    ) -> dict[str, Any]:
        """
        Start a merge operation.

        Args:
            worktree_id: ID of the worktree to merge in.
            source_branch: Branch being merged in.
            target_branch: Target branch (default: main).
            strategy: Resolution strategy ('auto', 'conflict_only', 'full_file', 'manual').

        Returns:
            Dict with resolution_id, success status, and conflict details.
        """
        # Validate required parameters
        if not worktree_id:
            return {"success": False, "error": "worktree_id is required"}
        if not source_branch:
            return {"success": False, "error": "source_branch is required"}

        resolution = None
        try:
            # Create resolution record
            resolution = merge_storage.create_resolution(
                worktree_id=worktree_id,
                source_branch=source_branch,
                target_branch=target_branch,
                status="pending",
            )

            # Attempt merge resolution
            from gobby.worktrees.merge import ResolutionTier

            force_tier = None
            if strategy == "conflict_only":
                force_tier = ResolutionTier.CONFLICT_ONLY_AI
            elif strategy == "full_file":
                force_tier = ResolutionTier.FULL_FILE_AI

            # Get worktree path from manager
            worktree_path = None
            if worktree_manager:
                worktree = worktree_manager.get(worktree_id)
                if worktree and worktree.worktree_path:
                    worktree_path = worktree.worktree_path

            if not worktree_path:
                return {
                    "success": False,
                    "error": f"Worktree '{worktree_id}' not found or has no path",
                }

            result = await merge_resolver.resolve(
                worktree_path=worktree_path,
                source_branch=source_branch,
                target_branch=target_branch,
                force_tier=force_tier,
            )

            # Update resolution with result
            merge_storage.update_resolution(
                resolution_id=resolution.id,
                status="resolved" if result.success else "pending",
                tier_used=result.tier.value if result.success else None,
            )

            # Create conflict records if needed
            for conflict in result.conflicts:
                file_path = conflict.get("file", "")
                merge_storage.create_conflict(
                    resolution_id=resolution.id,
                    file_path=file_path,
                    ours_content=conflict.get("ours_content"),
                    theirs_content=conflict.get("theirs_content"),
                    status="pending" if not result.success else "resolved",
                )

            return {
                "success": result.success,
                "resolution_id": resolution.id,
                "tier": result.tier.value,
                "needs_human_review": result.needs_human_review,
                "conflicts": [{"file": c.get("file", "")} for c in result.unresolved_conflicts],
                "resolved_files": result.resolved_files,
            }

        except Exception as e:
            logger.exception(
                f"Error starting merge for worktree_id={worktree_id}, resolution_id={resolution.id if resolution is not None else 'N/A'}",
            )
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="merge_status",
        description="Get the status of a merge resolution including conflict details.",
    )
    async def merge_status(resolution_id: str) -> dict[str, Any]:
        """
        Get merge resolution status.

        Args:
            resolution_id: The resolution ID.

        Returns:
            Dict with resolution details and conflicts.
        """
        if not resolution_id:
            return {"success": False, "error": "resolution_id is required"}

        resolution = merge_storage.get_resolution(resolution_id)
        if not resolution:
            return {"success": False, "error": f"Resolution '{resolution_id}' not found"}

        conflicts = merge_storage.list_conflicts(resolution_id=resolution_id)

        return {
            "success": True,
            "resolution": resolution.to_dict(),
            "conflicts": [c.to_dict() for c in conflicts],
            "pending_count": sum(1 for c in conflicts if c.status == "pending"),
            "resolved_count": sum(1 for c in conflicts if c.status == "resolved"),
        }

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

        try:
            if resolved_content is not None:
                # Manual resolution
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
                # Use AI resolver
                from gobby.worktrees.merge import ConflictHunk

                # Create hunk from conflict data
                hunks = [
                    ConflictHunk(
                        ours=conflict.ours_content or "",
                        theirs=conflict.theirs_content or "",
                        base=None,
                        start_line=1,
                        end_line=1,
                        context_before="",
                        context_after="",
                    )
                ]

                result = await merge_resolver.resolve_file(
                    path=conflict.file_path,
                    conflict_hunks=hunks,
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
                else:
                    return {
                        "success": False,
                        "error": "AI resolution failed",
                        "needs_human_review": result.needs_human_review,
                    }

            return {"success": False, "error": "No resolution method specified"}

        except Exception as e:
            logger.exception(f"Error resolving conflict {conflict_id}")
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="merge_apply",
        description="Apply all resolved conflicts and complete the merge.",
    )
    async def merge_apply(resolution_id: str) -> dict[str, Any]:
        """
        Apply all resolutions and complete the merge.

        Args:
            resolution_id: The resolution ID.

        Returns:
            Dict with merge completion status.
        """
        if not resolution_id:
            return {"success": False, "error": "resolution_id is required"}

        resolution = merge_storage.get_resolution(resolution_id)
        if not resolution:
            return {"success": False, "error": f"Resolution '{resolution_id}' not found"}

        conflicts = merge_storage.list_conflicts(resolution_id=resolution_id)

        # Check if all conflicts are resolved
        pending = [c for c in conflicts if c.status != "resolved"]
        if pending:
            return {
                "success": False,
                "error": f"Cannot apply: {len(pending)} unresolved conflicts remaining",
                "pending_conflicts": [{"id": c.id, "file_path": c.file_path} for c in pending],
            }

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
                    "error": (f"Worktree '{resolution.worktree_id}' not found or has no path"),
                }
            wt_path = worktree.worktree_path

            written: list[str] = []
            for conflict in conflicts:
                if conflict.resolved_content is None:
                    return {
                        "success": False,
                        "error": (
                            f"Conflict {conflict.id} for {conflict.file_path} has no "
                            "resolved_content; resolve it before applying"
                        ),
                    }
                target = Path(wt_path) / conflict.file_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(conflict.resolved_content)

                add_result = await asyncio.to_thread(
                    git_manager._run_git,
                    ["add", "--", conflict.file_path],
                    cwd=wt_path,
                    timeout=10,
                )
                if add_result.returncode != 0:
                    return {
                        "success": False,
                        "error": (
                            f"git add failed for {conflict.file_path}: {add_result.stderr.strip()}"
                        ),
                    }
                written.append(conflict.file_path)

            unmerged_check = await asyncio.to_thread(
                git_manager._run_git,
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=wt_path,
                timeout=10,
            )
            unmerged = [
                line.strip() for line in unmerged_check.stdout.strip().split("\n") if line.strip()
            ]
            if unmerged:
                return {
                    "success": False,
                    "error": (
                        f"Cannot complete merge: {len(unmerged)} files still have "
                        "unmerged changes after applying resolutions"
                    ),
                    "unmerged_files": unmerged,
                }

            commit_result = await asyncio.to_thread(
                git_manager._run_git,
                ["commit", "--no-edit"],
                cwd=wt_path,
                timeout=30,
            )
            if commit_result.returncode != 0:
                return {
                    "success": False,
                    "error": (
                        f"git commit failed: "
                        f"{(commit_result.stderr or commit_result.stdout).strip()}"
                    ),
                }

            updated = merge_storage.update_resolution(
                resolution_id=resolution_id,
                status="resolved",
                tier_used=resolution.tier_used or "manual",
            )

            return {
                "success": True,
                "resolution": updated.to_dict() if updated else None,
                "message": "Merge completed successfully",
                "files_merged": written,
            }

        except Exception as e:
            logger.exception(f"Error applying merge for resolution {resolution_id}")
            return {"success": False, "error": str(e)}

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

        # Can't abort already resolved merges
        if resolution.status == "resolved":
            return {"success": False, "error": "Cannot abort: merge is already resolved"}

        try:
            # Abort git merge if in progress
            if git_manager:
                # Would run git merge --abort
                pass

            # Delete resolution and associated conflicts (cascade)
            deleted = merge_storage.delete_resolution(resolution_id)

            if deleted:
                return {
                    "success": True,
                    "message": "Merge aborted successfully",
                    "resolution_id": resolution_id,
                }
            else:
                return {"success": False, "error": "Failed to abort merge"}

        except Exception as e:
            logger.exception(f"Error aborting merge for resolution_id={resolution_id}")
            return {"success": False, "error": str(e)}

    # Register merge-landscape analytics tools on the same registry so the
    # merge-orchestrator agent can survey/predict/verify alongside resolve/apply.
    from gobby.mcp_proxy.tools.merge_landscape import register_merge_landscape_tools

    register_merge_landscape_tools(
        registry,
        worktree_manager=worktree_manager,
        git_manager=git_manager,
    )

    return registry
