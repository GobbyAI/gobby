"""Registration for the merge_apply MCP tool."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.merge_direct import _complete_direct_merge, _dirty_worktree_result
from gobby.mcp_proxy.tools.merge_git_state import (
    current_branch,
    merge_head_exists,
    rev_parse_head,
    source_branch_validation_error,
)
from gobby.mcp_proxy.tools.merge_github_protection import git_output
from gobby.mcp_proxy.tools.merge_resolve_locks import try_acquire_resolve_lock
from gobby.storage.merge_resolutions import MergeResolutionManager
from gobby.worktrees.git import WorktreeGitManager
from gobby.worktrees.merge.resolver import assert_marker_free

logger = logging.getLogger(__name__)


def register_merge_apply_tool(
    registry: InternalToolRegistry,
    *,
    merge_storage: MergeResolutionManager,
    git_manager: WorktreeGitManager | None = None,
    worktree_manager: Any | None = None,
) -> None:
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

        resolve_lock: asyncio.Lock | None = None
        try:
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

            conflicts = merge_storage.list_conflicts(resolution_id=resolution_id)

            pending = [c for c in conflicts if c.status != "resolved"]
            if pending:
                return {
                    "success": False,
                    "error": f"Cannot apply: {len(pending)} unresolved conflicts remaining",
                    "pending_conflicts": [{"id": c.id, "file_path": c.file_path} for c in pending],
                }

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
                if conflicts:
                    return {
                        "success": False,
                        "error": (
                            "Cannot apply resolved conflicts: git has no MERGE_HEAD for "
                            "this resolution"
                        ),
                    }

            validated_content: dict[str, str] = {}
            for conflict in conflicts:
                content = conflict.resolved_content
                if content is None:
                    return {
                        "success": False,
                        "error": (
                            f"Conflict {conflict.id} for {conflict.file_path} has no "
                            "resolved_content; resolve it before applying"
                        ),
                    }
                try:
                    assert_marker_free(content)
                except ValueError as exc:
                    return {
                        "success": False,
                        "error": f"Cannot apply {conflict.file_path}: {exc}",
                    }
                validated_content[conflict.id] = content

            written: list[str] = []
            for conflict in conflicts:
                target = Path(wt_path) / conflict.file_path
                await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(
                    target.write_text,
                    validated_content[conflict.id],
                    encoding="utf-8",
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

                direct_result = await _complete_direct_merge(git_manager, resolution, wt_path)
                if not direct_result["success"]:
                    return direct_result

                updated = merge_storage.update_resolution(
                    resolution_id=resolution_id,
                    status="resolved",
                    tier_used=resolution.tier_used or "manual",
                )

                result = {
                    "success": True,
                    "resolution": updated.to_dict() if updated else None,
                    "message": "Merge completed successfully",
                    "files_merged": written,
                    "merge_sha": direct_result["merge_sha"],
                    "commit_sha": direct_result["merge_sha"],
                    "merge_strategy": direct_result["merge_strategy"],
                    "direct_merge": True,
                }
                if "warning" in direct_result:
                    result["warning"] = direct_result["warning"]
                    result["dirty_files"] = direct_result.get("dirty_files", [])
                return result

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

            dirty_result = await _dirty_worktree_result(
                git_manager,
                wt_path,
                after_merge=True,
            )

            updated = merge_storage.update_resolution(
                resolution_id=resolution_id,
                status="resolved",
                tier_used=resolution.tier_used or "manual",
            )

            result = {
                "success": True,
                "resolution": updated.to_dict() if updated else None,
                "message": "Merge completed successfully",
                "files_merged": written,
                "merge_sha": merge_sha,
                "commit_sha": merge_sha,
                "direct_merge": False,
            }
            if dirty_result is not None:
                result["warning"] = dirty_result["error"]
                result["dirty_files"] = dirty_result.get("dirty_files", [])
            return result

        except Exception as e:
            logger.exception("Error applying merge for resolution %s", resolution_id)
            return {"success": False, "error": str(e)}
        finally:
            if resolve_lock is not None and resolve_lock.locked():
                resolve_lock.release()
