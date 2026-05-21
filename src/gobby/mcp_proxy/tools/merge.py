"""Internal MCP tools for Gobby merge resolution."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_conflict_hydration import (
    collect_git_conflicts,
    conflict_hunks_for_ai,
    normalized_status_conflicts,
    store_missing_conflicts,
)
from gobby.mcp_proxy.tools.merge_git_state import (
    current_branch,
    merge_head_exists,
    resolved_reuse_error,
    rev_parse_head,
    source_branch_validation_error,
)
from gobby.mcp_proxy.tools.merge_github_protection import (
    git_output,
    github_token,
    parse_github_remote,
    parse_protection_response,
    protection_payload,
    push_dry_run_probe,
)
from gobby.mcp_proxy.tools.merge_landscape import register_merge_landscape_tools
from gobby.mcp_proxy.tools.merge_resolve_locks import try_acquire_resolve_lock
from gobby.storage.merge_resolutions import ConflictStatus

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.merge_resolutions import MergeResolutionManager
    from gobby.worktrees.git import WorktreeGitManager
    from gobby.worktrees.merge import MergeResolver

logger = logging.getLogger(__name__)
_GIT_NO_FF_TIER = "git_no_ff"
_NO_FF_STRATEGIES = {"no-ff", "no_ff"}


def _strategy_requests_no_ff(strategy: str) -> bool:
    return strategy.strip().lower() in _NO_FF_STRATEGIES


def _status_path_is_gobby_only(pathspec: str) -> bool:
    paths = [part.strip() for part in pathspec.split(" -> ")]
    return all(path == ".gobby" or path.startswith(".gobby/") for path in paths)


def _non_gobby_status_lines(status_output: str) -> list[str]:
    dirty: list[str] = []
    for line in status_output.splitlines():
        if not line:
            continue
        pathspec = line[3:] if len(line) > 3 else line
        if not _status_path_is_gobby_only(pathspec):
            dirty.append(line)
    return dirty


def create_merge_registry(
    merge_storage: MergeResolutionManager,
    merge_resolver: MergeResolver,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
    db: HubDatabase | None = None,
) -> InternalToolRegistry:
    """
    Create a merge tool registry with all merge-related tools.

    Args:
        merge_storage: MergeResolutionManager for database operations.
        merge_resolver: MergeResolver for AI-powered conflict resolution.
        git_manager: WorktreeGitManager for git operations.
        worktree_manager: LocalWorktreeManager for resolving worktree paths.
        db: Local database for resolving GitHub tokens.

    Returns:
        InternalToolRegistry with all merge tools registered.
    """
    registry = InternalToolRegistry(
        name="gobby-merge",
        description="AI-powered merge conflict resolution - start merges, resolve conflicts, and apply resolutions",
    )

    async def _existing_resolution_start_response(
        resolution: Any,
        *,
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
                logger.info(
                    "Invalidated stale merge resolution %s: %s", resolution.id, stale_reason
                )
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

    async def _complete_direct_merge(resolution: Any, wt_path: str) -> dict[str, Any]:
        if git_manager is None:
            return {"success": False, "error": "git_manager not configured"}

        repo_path = str(getattr(git_manager, "repo_path", None) or wt_path)
        original_branch_result = await asyncio.to_thread(
            git_manager.run_git_command,
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            timeout=10,
        )
        if original_branch_result.returncode != 0:
            return {
                "success": False,
                "error": f"Failed to determine current branch: {git_output(original_branch_result)}",
            }

        original_branch = original_branch_result.stdout.strip()
        target_branch = resolution.target_branch
        source_branch = resolution.source_branch
        restore_original = original_branch and original_branch != target_branch
        strategy_name = "no-ff" if resolution.tier_used == _GIT_NO_FF_TIER else "ff-only"

        try:
            if restore_original:
                checkout_result = await asyncio.to_thread(
                    git_manager.run_git_command,
                    ["checkout", target_branch],
                    cwd=repo_path,
                    timeout=30,
                )
                if checkout_result.returncode != 0:
                    return {
                        "success": False,
                        "error": (
                            f"Failed to checkout target branch '{target_branch}': "
                            f"{git_output(checkout_result)}"
                        ),
                    }

            merge_args = (
                ["merge", "--no-ff", "--no-edit", source_branch]
                if strategy_name == "no-ff"
                else ["merge", "--ff-only", source_branch]
            )
            merge_result = await asyncio.to_thread(
                git_manager.run_git_command,
                merge_args,
                cwd=repo_path,
                timeout=60,
            )
            if merge_result.returncode != 0:
                return {
                    "success": False,
                    "error": f"Direct {strategy_name} merge failed: {git_output(merge_result)}",
                    "merge_strategy": strategy_name,
                }

            merge_sha = await rev_parse_head(git_manager, repo_path)
            if not merge_sha:
                return {
                    "success": False,
                    "error": "Direct merge completed but HEAD could not be resolved",
                    "merge_strategy": strategy_name,
                }

            dirty_result = await _dirty_worktree_result(repo_path)
            if dirty_result is not None:
                dirty_result.update(
                    {
                        "merge_sha": merge_sha,
                        "commit_sha": merge_sha,
                        "merge_strategy": strategy_name,
                    }
                )
                return dirty_result

            return {
                "success": True,
                "merge_sha": merge_sha,
                "merge_strategy": strategy_name,
                "merge_output": (merge_result.stdout or merge_result.stderr or "").strip(),
            }
        finally:
            if restore_original:
                restore_result = await asyncio.to_thread(
                    git_manager.run_git_command,
                    ["checkout", original_branch],
                    cwd=repo_path,
                    timeout=30,
                )
                if restore_result.returncode != 0:
                    logger.warning(
                        "Failed to restore branch %s after direct merge: %s",
                        original_branch,
                        git_output(restore_result),
                    )

    async def _dirty_worktree_result(wt_path: str) -> dict[str, Any] | None:
        if git_manager is None:
            return {"success": False, "error": "git_manager not configured for clean-tree check"}

        status_result = await asyncio.to_thread(
            git_manager.run_git_command,
            ["status", "--porcelain"],
            cwd=wt_path,
            timeout=10,
        )
        if status_result.returncode != 0:
            return {
                "success": False,
                "error": f"git status failed after merge commit: {git_output(status_result)}",
            }

        dirty_files = _non_gobby_status_lines(status_result.stdout)
        if not dirty_files:
            return None

        return {
            "success": False,
            "error": "merge completed but worktree is dirty",
            "dirty_files": dirty_files,
        }

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
                        existing, worktree_path=worktree_path
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
                            resolution, worktree_path=worktree_path
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

            from gobby.worktrees.merge import ResolutionTier

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
                f"Error starting merge for worktree_id={worktree_id}, resolution_id={resolution.id if resolution is not None else 'N/A'}",
            )
            return {"success": False, "error": str(e)}

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
                merge_storage.update_resolution(resolution_id=resolution_id, status="pending")
                or resolution
            )

        return {
            "success": True,
            "resolution": resolution.to_dict(),
            "conflicts": conflict_payloads,
            "pending_count": pending_count,
            "resolved_count": resolved_count,
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

        resolve_lock: asyncio.Lock | None = None
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
                else:
                    return {
                        "success": False,
                        "error": "AI resolution failed",
                        "needs_human_review": result.needs_human_review,
                        "failure_reason": result.failure_reason,
                    }

            return {"success": False, "error": "No resolution method specified"}

        except Exception as e:
            logger.exception(f"Error resolving conflict {conflict_id}")
            return {"success": False, "error": str(e)}
        finally:
            if resolve_lock is not None and resolve_lock.locked():
                resolve_lock.release()

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
            worktree_branch = (
                getattr(worktree, "branch_name", None)
                if isinstance(getattr(worktree, "branch_name", None), str)
                else None
            )
            merge_in_progress = await merge_head_exists(git_manager, wt_path)
            if merge_in_progress:
                branch = await current_branch(git_manager, wt_path)
                if worktree_branch and branch and branch != worktree_branch:
                    return {
                        "success": False,
                        "error": (
                            "Cannot apply active merge: current branch "
                            f"'{branch}' does not match worktree branch '{worktree_branch}'"
                        ),
                    }
            else:
                validation_error = await source_branch_validation_error(
                    git_manager=git_manager,
                    worktree_path=wt_path,
                    worktree_branch=worktree_branch,
                    source_branch=resolution.source_branch,
                    target_branch=resolution.target_branch,
                )
                if validation_error:
                    return {"success": False, "error": validation_error}

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
                await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(
                    target.write_text, conflict.resolved_content, encoding="utf-8"
                )

                add_result = await asyncio.to_thread(
                    git_manager.stage_files,
                    [conflict.file_path],
                    cwd=wt_path,
                )
                if add_result.returncode != 0:
                    return {
                        "success": False,
                        "error": (
                            f"git add failed for {conflict.file_path}: {add_result.stderr.strip()}"
                        ),
                    }
                written.append(conflict.file_path)

            unmerged = await asyncio.to_thread(git_manager.get_unmerged_files, cwd=wt_path)
            if unmerged:
                return {
                    "success": False,
                    "error": (
                        f"Cannot complete merge: {len(unmerged)} files still have "
                        "unmerged changes after applying resolutions"
                    ),
                    "unmerged_files": unmerged,
                }

            if not merge_in_progress:
                if written:
                    return {
                        "success": False,
                        "error": (
                            "Cannot complete merge: resolved files were applied but git has no "
                            "MERGE_HEAD"
                        ),
                    }

                direct_result = await _complete_direct_merge(resolution, wt_path)
                if not direct_result["success"]:
                    return direct_result

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
                    "merge_sha": direct_result["merge_sha"],
                    "commit_sha": direct_result["merge_sha"],
                    "merge_strategy": direct_result["merge_strategy"],
                    "direct_merge": True,
                }

            commit_result = await asyncio.to_thread(
                git_manager.run_git_command,
                ["commit", "--no-edit"],
                cwd=wt_path,
                timeout=30,
            )
            if commit_result.returncode != 0:
                return {
                    "success": False,
                    "error": f"git commit failed: {git_output(commit_result)}",
                }

            merge_sha = await rev_parse_head(git_manager, wt_path)
            if not merge_sha:
                return {"success": False, "error": "Merge committed but HEAD could not be resolved"}

            dirty_result = await _dirty_worktree_result(wt_path)
            if dirty_result is not None:
                dirty_result.update({"merge_sha": merge_sha, "commit_sha": merge_sha})
                return dirty_result

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
                "merge_sha": merge_sha,
                "commit_sha": merge_sha,
                "direct_merge": False,
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

    @registry.tool(
        name="probe_branch_protection",
        description="Probe whether a target branch should be delivered through a GitHub PR.",
    )
    async def probe_branch_protection(
        repo_path: str | None = None,
        branch: str = "main",
        worktree_id: str | None = None,
    ) -> dict[str, Any]:
        """Probe GitHub branch protection and return PR gating requirements."""
        effective_repo_path = repo_path
        if not effective_repo_path and worktree_id and worktree_manager is not None:
            worktree = worktree_manager.get(worktree_id)
            if worktree is not None:
                effective_repo_path = worktree.worktree_path
                branch = branch or worktree.base_branch
        if not effective_repo_path:
            return {
                "success": False,
                "error": "repo_path or resolvable worktree_id is required",
            }

        remote_url: str | None = None
        if git_manager is not None:
            remote = await asyncio.to_thread(
                git_manager.run_git_command,
                ["remote", "get-url", "origin"],
                cwd=effective_repo_path,
                timeout=10,
            )
            if remote.returncode == 0:
                remote_url = remote.stdout.strip()
        if not remote_url:
            from gobby.utils.git import get_github_url

            remote_url = get_github_url(effective_repo_path)
        if not remote_url:
            return {"success": False, "error": "No origin remote found"}

        parsed = parse_github_remote(remote_url)
        if parsed is None:
            return {
                "success": False,
                "error": f"Origin remote is not a github.com repository: {remote_url}",
            }
        owner, repo = parsed

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = github_token(db)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        api_url = f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}/protection"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(api_url, headers=headers)
        except httpx.HTTPError as exc:
            return await push_dry_run_probe(
                repo_path=effective_repo_path,
                owner=owner,
                repo=repo,
                branch=branch,
                git_manager=git_manager,
                source="push_dry_run_after_api_error",
                error=str(exc),
            )

        if response.status_code == 200:
            return parse_protection_response(owner, repo, branch, response.json())
        if response.status_code == 404:
            return protection_payload(
                owner=owner,
                repo=repo,
                branch=branch,
                source="github_api",
                requires_pr=False,
            )

        if response.status_code in {401, 403}:
            fallback_source = f"push_dry_run_after_{response.status_code}"
            return await push_dry_run_probe(
                repo_path=effective_repo_path,
                owner=owner,
                repo=repo,
                branch=branch,
                git_manager=git_manager,
                source=fallback_source,
                error=response.text.strip(),
            )

        return await push_dry_run_probe(
            repo_path=effective_repo_path,
            owner=owner,
            repo=repo,
            branch=branch,
            git_manager=git_manager,
            source=f"push_dry_run_after_{response.status_code}",
            error=response.text.strip(),
        )

    register_merge_landscape_tools(
        registry,
        worktree_manager=worktree_manager,
        git_manager=git_manager,
        merge_storage=merge_storage,
    )

    return registry
