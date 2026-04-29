"""Worktree sync and merge tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, cast

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.mcp_proxy.tools.worktrees._helpers import resolve_project_context
from gobby.mcp_proxy.tools.worktrees._merge_state import is_branch_ancestor

logger = logging.getLogger(__name__)


def create_sync_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with worktree sync/merge tools.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with sync and merge tools
    """
    registry = InternalToolRegistry(
        name="gobby-worktrees-sync",
        description="Worktree sync and merge operations",
    )

    @registry.tool(
        name="sync_worktree",
        description="Sync a worktree with the main branch.",
    )
    async def sync_worktree(
        worktree_id: str,
        strategy: str = "merge",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Sync a worktree with the main branch.

        Args:
            worktree_id: The worktree ID to sync.
            strategy: Sync strategy ('merge' or 'rebase').
            project_path: Path to project directory (pass cwd from CLI).

        Returns:
            Dict with sync result.
        """
        resolved_git_mgr, _, error = resolve_project_context(
            project_path, ctx.git_manager, ctx.project_id
        )
        if error:
            return {"success": False, "error": error}

        if resolved_git_mgr is None:
            return {
                "success": False,
                "error": "Git manager not configured and no project_path provided.",
            }

        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        if strategy not in ("rebase", "merge"):
            return {
                "success": False,
                "error": f"Invalid strategy '{strategy}'. Must be 'rebase' or 'merge'.",
            }

        strategy_literal = cast(Literal["rebase", "merge"], strategy)

        result = await asyncio.to_thread(
            resolved_git_mgr.sync_from_main,
            worktree.worktree_path,
            base_branch=worktree.base_branch,
            strategy=strategy_literal,
        )

        if not result.success:
            return {"success": False, "error": result.error or "Sync failed"}

        ctx.worktree_storage.update(worktree_id)

        return {
            "success": True,
            "message": result.message,
            "output": result.output,
            "strategy": strategy,
        }

    @registry.tool(
        name="merge_worktree",
        description="Merge a worktree's branch into its base branch (or a specified target).",
    )
    async def merge_worktree(
        worktree_id: str,
        source_branch: str | None = None,
        target_branch: str | None = None,
        push: bool = False,
        prefer_remote: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Merge and optionally push from worktree -- fully isolated, never touches main repo.

        1. Fetch latest in the worktree
        2. Merge target INTO source branch (in worktree -- conflicts resolved here)
        3. (Optional) Push source to origin as target (git push origin source:target)

        The main repo is never touched. All operations use cwd=worktree_path.

        Args:
            worktree_id: The worktree ID to merge.
            source_branch: Agent's working branch (defaults to worktree's branch_name).
            target_branch: Branch to merge into (defaults to worktree's base_branch).
            push: If True, push source branch to origin as target after merge.
            prefer_remote: If True, merge origin/target_branch instead of local target_branch.
            project_path: Path to project directory (pass cwd from CLI).

        Returns:
            Dict with source_branch, target_branch on success.
        """
        resolved_git_mgr, _, error = resolve_project_context(
            project_path, ctx.git_manager, ctx.project_id
        )
        if error:
            return {"success": False, "error": error}
        if resolved_git_mgr is None:
            return {"success": False, "error": "Git manager not available"}

        worktree = ctx.worktree_storage.get(worktree_id)
        if not worktree:
            return {"success": False, "error": f"Worktree '{worktree_id}' not found"}

        effective_source = source_branch or worktree.branch_name
        raw_merge_target = target_branch or worktree.base_branch
        remote_target_requested = raw_merge_target.startswith("origin/")
        merge_target = raw_merge_target.removeprefix("origin/")
        use_remote_target = prefer_remote or remote_target_requested
        wt_path = worktree.worktree_path

        # Step 1: Fetch latest in the worktree
        fetch_result = await asyncio.to_thread(
            resolved_git_mgr._run_git, ["fetch", "origin"], cwd=wt_path, timeout=60
        )
        if fetch_result.returncode != 0:
            logger.warning(f"Fetch failed in worktree (non-fatal): {fetch_result.stderr.strip()}")

        remote_merge_ref = f"origin/{merge_target}"
        local_ref_result = await asyncio.to_thread(
            resolved_git_mgr._run_git,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{merge_target}"],
            cwd=wt_path,
            timeout=10,
        )
        remote_ref_result = await asyncio.to_thread(
            resolved_git_mgr._run_git,
            ["show-ref", "--verify", "--quiet", f"refs/remotes/{remote_merge_ref}"],
            cwd=wt_path,
            timeout=10,
        )
        local_ref_exists = local_ref_result.returncode == 0
        remote_ref_exists = remote_ref_result.returncode == 0
        if use_remote_target:
            merge_ref = remote_merge_ref if remote_ref_exists else None
        else:
            merge_ref = None
            if local_ref_exists:
                merge_ref = merge_target
            elif remote_ref_exists:
                merge_ref = remote_merge_ref
        if merge_ref is None:
            return {
                "success": False,
                "error": (
                    f"Target branch '{merge_target}' not found as local branch "
                    f"or remote ref '{remote_merge_ref}'"
                ),
                "worktree_path": wt_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
            }

        # Step 2: Stash dirty .gobby/ sync files to prevent merge blocking
        # Compare stash list before/after to reliably detect if a stash was created
        # (same pattern as merge_clone)
        stash_created = False
        stash_list_before = await asyncio.to_thread(
            resolved_git_mgr._run_git, ["stash", "list"], cwd=wt_path, timeout=10
        )
        stash_push = await asyncio.to_thread(
            resolved_git_mgr._run_git,
            ["stash", "push", "-m", "gobby-merge: auto-stash sync files", "--", ".gobby/"],
            cwd=wt_path,
            timeout=10,
        )
        if stash_push.returncode == 0:
            stash_list_after = await asyncio.to_thread(
                resolved_git_mgr._run_git, ["stash", "list"], cwd=wt_path, timeout=10
            )
            stash_created = stash_list_after.stdout != stash_list_before.stdout

        # Step 3: Merge target INTO source branch (in worktree)
        # Wrapped in try/finally to ensure stashed .gobby/ files are always restored
        async def _restore_stash() -> None:
            """Restore stashed .gobby/ files if any were stashed."""
            if stash_created:
                pop_result = await asyncio.to_thread(
                    resolved_git_mgr._run_git, ["stash", "pop"], cwd=wt_path, timeout=10
                )
                if pop_result.returncode != 0:
                    logger.warning(
                        f"Failed to restore stashed .gobby/ files: "
                        f"{pop_result.stderr or pop_result.stdout}"
                    )

        async def _source_is_merged_into_target() -> bool:
            return await asyncio.to_thread(
                is_branch_ancestor,
                resolved_git_mgr,
                effective_source,
                merge_target,
                cwd=wt_path,
            )

        try:
            merge_result = await asyncio.to_thread(
                resolved_git_mgr._run_git,
                ["merge", merge_ref, "--no-edit"],
                cwd=wt_path,
                timeout=60,
            )

            if merge_result.returncode != 0:
                # Detect unmerged (conflicted) files via git index — more reliable
                # than parsing human-readable merge output for "CONFLICT" strings
                unmerged_result = await asyncio.to_thread(
                    resolved_git_mgr._run_git,
                    ["diff", "--name-only", "--diff-filter=U"],
                    cwd=wt_path,
                    timeout=10,
                )
                conflicted_files = [
                    f.strip() for f in unmerged_result.stdout.strip().split("\n") if f.strip()
                ]
                if conflicted_files:
                    # Auto-resolve trivial conflicts (.gobby/*.jsonl)
                    from gobby.worktrees.merge.resolver import auto_resolve_trivial_conflicts

                    remaining = await auto_resolve_trivial_conflicts(conflicted_files, wt_path)

                    if not remaining:
                        # All conflicts were trivial — commit the merge and continue
                        commit_result = await asyncio.to_thread(
                            resolved_git_mgr._run_git,
                            ["commit", "--no-edit"],
                            cwd=wt_path,
                            timeout=30,
                        )
                        if commit_result.returncode == 0:
                            git_merged = await _source_is_merged_into_target()
                            if git_merged:
                                ctx.worktree_storage.mark_merged(worktree_id)
                            return {
                                "success": True,
                                "message": (
                                    f"Merged (auto-resolved {len(conflicted_files)} trivial conflict(s))"
                                ),
                                "worktree_path": wt_path,
                                "source_branch": effective_source,
                                "target_branch": merge_target,
                                "merged": git_merged,
                                "pushed": False,
                                "auto_resolved": conflicted_files,
                            }

                    # Still have real conflicts — abort and report
                    await asyncio.to_thread(
                        resolved_git_mgr._run_git,
                        ["merge", "--abort"],
                        cwd=wt_path,
                        timeout=10,
                    )
                    return {
                        "success": False,
                        "has_conflicts": True,
                        "merged": False,
                        "conflicted_files": remaining,
                        "auto_resolved": [f for f in conflicted_files if f not in remaining],
                        "worktree_path": wt_path,
                        "message": (
                            f"Merge conflicts detected in {len(remaining)} file(s) "
                            f"({len(conflicted_files) - len(remaining)} trivial auto-resolved). "
                            "Use gobby-merge tools to resolve."
                        ),
                    }
                merge_output = merge_result.stdout + merge_result.stderr
                return {
                    "success": False,
                    "has_conflicts": False,
                    "worktree_path": wt_path,
                    "error": merge_output.strip(),
                }

            # Step 4 (optional): Push source branch to origin as target
            if push:
                push_result = await asyncio.to_thread(
                    resolved_git_mgr._run_git,
                    ["push", "--no-verify", "origin", f"{effective_source}:{merge_target}"],
                    cwd=wt_path,
                    timeout=60,
                )
                if push_result.returncode != 0:
                    return {
                        "success": False,
                        "error": f"Push failed: {push_result.stderr.strip()}",
                        "merge_succeeded": True,
                        "worktree_path": wt_path,
                        "source_branch": effective_source,
                        "target_branch": merge_target,
                    }
                fetch_target = await asyncio.to_thread(
                    resolved_git_mgr._run_git,
                    ["fetch", "origin", merge_target],
                    cwd=wt_path,
                    timeout=60,
                )
                if fetch_target.returncode != 0:
                    logger.warning(
                        "Fetch after push failed in worktree (non-fatal): %s",
                        fetch_target.stderr.strip(),
                    )

            git_merged = await _source_is_merged_into_target()
            if git_merged:
                ctx.worktree_storage.mark_merged(worktree_id)
            elif push:
                return {
                    "success": False,
                    "error": (
                        f"Push succeeded, but branch '{effective_source}' is not an ancestor "
                        f"of '{merge_target}'"
                    ),
                    "merge_succeeded": True,
                    "push_succeeded": True,
                    "merged": False,
                    "worktree_path": wt_path,
                    "source_branch": effective_source,
                    "target_branch": merge_target,
                }

            result = {
                "success": True,
                "message": (
                    f"Merged and {'pushed' if push else 'ready to push'}"
                    if git_merged
                    else "Worktree branch synced with target; target branch not updated"
                ),
                "worktree_path": wt_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
                "merged": git_merged,
                "pushed": push,
            }
            if not push:
                result["push_command"] = (
                    f"git push --no-verify origin {effective_source}:{merge_target}"
                )
            return result
        finally:
            await _restore_stash()

    return registry
