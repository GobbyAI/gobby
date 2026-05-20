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


def _worktree_path_for_branch(git_manager: Any, branch_name: str) -> str | None:
    """Return the path where a branch is already checked out, if known."""
    list_worktrees = getattr(git_manager, "list_worktrees", None)
    if not callable(list_worktrees):
        return None

    try:
        for worktree in list_worktrees():
            if getattr(worktree, "branch", None) == branch_name:
                path = getattr(worktree, "path", None)
                if isinstance(path, str) and path:
                    return path
    except Exception as exc:
        logger.debug("Failed to inspect git worktrees for branch %s: %s", branch_name, exc)
    return None


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
        """Merge a worktree branch into the local target branch.

        This is local-only delivery. Remote publication must go through the PR
        delivery path, not worktree merge automation.

        Args:
            worktree_id: The worktree ID to merge.
            source_branch: Agent's working branch (defaults to worktree's branch_name).
            target_branch: Branch to merge into (defaults to worktree's base_branch).
            push: Unsupported. Worktree merge never pushes to remote.
            prefer_remote: Unsupported. Worktree merge targets the local branch.
            project_path: Path to project directory (pass cwd from CLI).

        Returns:
            Dict with source_branch, target_branch, and final target merge_sha on success.
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

        if push:
            return {
                "success": False,
                "error": (
                    "merge_worktree is local-only and never pushes to remote. "
                    "Use the explicit PR delivery flow for remote publication."
                ),
            }
        if prefer_remote:
            return {
                "success": False,
                "error": (
                    "merge_worktree merges into the local target branch; "
                    "remote target refs are only valid in the PR delivery flow."
                ),
            }

        effective_source = source_branch or worktree.branch_name
        raw_merge_target = target_branch or worktree.base_branch
        if raw_merge_target.startswith("origin/"):
            return {
                "success": False,
                "error": (
                    "merge_worktree requires a local target branch, "
                    f"got remote ref '{raw_merge_target}'"
                ),
            }
        merge_target = raw_merge_target
        wt_path = worktree.worktree_path
        repo_path = str(resolved_git_mgr.repo_path)
        target_worktree_path = await asyncio.to_thread(
            _worktree_path_for_branch, resolved_git_mgr, merge_target
        )
        merge_cwd = target_worktree_path or repo_path

        target_ref_result = await asyncio.to_thread(
            resolved_git_mgr.run_git_command,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{merge_target}"],
            cwd=repo_path,
            timeout=10,
        )
        if target_ref_result.returncode != 0:
            return {
                "success": False,
                "error": f"Local target branch '{merge_target}' not found",
                "worktree_path": wt_path,
                "project_path": repo_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
            }

        source_ref_result = await asyncio.to_thread(
            resolved_git_mgr.run_git_command,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{effective_source}"],
            cwd=repo_path,
            timeout=10,
        )
        if source_ref_result.returncode != 0:
            return {
                "success": False,
                "error": f"Local source branch '{effective_source}' not found",
                "worktree_path": wt_path,
                "project_path": repo_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
            }

        status_result = await asyncio.to_thread(
            resolved_git_mgr.run_git_command,
            ["status", "--porcelain"],
            cwd=merge_cwd,
            timeout=10,
        )
        if status_result.returncode != 0:
            return {
                "success": False,
                "error": f"Failed to inspect target checkout: {status_result.stderr.strip()}",
                "worktree_path": wt_path,
                "project_path": repo_path,
                "target_worktree_path": target_worktree_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
            }
        non_gobby_dirty = _non_gobby_status_lines(status_result.stdout)
        if non_gobby_dirty:
            return {
                "success": False,
                "error": "Target checkout has uncommitted non-.gobby changes",
                "dirty_files": non_gobby_dirty,
                "worktree_path": wt_path,
                "project_path": repo_path,
                "target_worktree_path": target_worktree_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
            }

        current_branch_result = await asyncio.to_thread(
            resolved_git_mgr.run_git_command,
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=merge_cwd,
            timeout=10,
        )
        if current_branch_result.returncode != 0:
            return {
                "success": False,
                "error": f"Failed to determine current branch: {current_branch_result.stderr.strip()}",
                "worktree_path": wt_path,
                "project_path": repo_path,
                "target_worktree_path": target_worktree_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
            }
        original_branch = current_branch_result.stdout.strip()
        if target_worktree_path and original_branch != merge_target:
            return {
                "success": False,
                "error": (
                    f"Target branch '{merge_target}' is registered at "
                    f"'{target_worktree_path}', but that checkout is on '{original_branch}'"
                ),
                "worktree_path": wt_path,
                "project_path": repo_path,
                "target_worktree_path": target_worktree_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
            }
        checked_out_target = original_branch == merge_target

        # Stash dirty .gobby/ sync files to prevent merge blocking.
        # Compare stash list before/after to reliably detect if a stash was created
        # (same pattern as merge_clone).
        stash_created = False
        stash_list_before = await asyncio.to_thread(
            resolved_git_mgr.run_git_command, ["stash", "list"], cwd=merge_cwd, timeout=10
        )
        stash_push = await asyncio.to_thread(
            resolved_git_mgr.run_git_command,
            ["stash", "push", "-m", "gobby-merge: auto-stash sync files", "--", ".gobby/"],
            cwd=merge_cwd,
            timeout=10,
        )
        if stash_push.returncode == 0:
            stash_list_after = await asyncio.to_thread(
                resolved_git_mgr.run_git_command, ["stash", "list"], cwd=merge_cwd, timeout=10
            )
            stash_created = stash_list_after.stdout != stash_list_before.stdout

        async def _restore_stash() -> None:
            """Restore stashed .gobby/ files if any were stashed."""
            if stash_created:
                pop_result = await asyncio.to_thread(
                    resolved_git_mgr.run_git_command, ["stash", "pop"], cwd=merge_cwd, timeout=10
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
                cwd=merge_cwd,
            )

        try:
            if original_branch != merge_target:
                checkout_result = await asyncio.to_thread(
                    resolved_git_mgr.run_git_command,
                    ["checkout", merge_target],
                    cwd=merge_cwd,
                    timeout=30,
                )
                if checkout_result.returncode != 0:
                    return {
                        "success": False,
                        "error": (
                            f"Failed to checkout local target branch '{merge_target}': "
                            f"{checkout_result.stderr.strip()}"
                        ),
                        "worktree_path": wt_path,
                        "project_path": repo_path,
                        "target_worktree_path": target_worktree_path,
                        "source_branch": effective_source,
                        "target_branch": merge_target,
                    }
                checked_out_target = True

            merge_result = await asyncio.to_thread(
                resolved_git_mgr.run_git_command,
                ["merge", effective_source, "--no-edit"],
                cwd=merge_cwd,
                timeout=60,
            )
            auto_resolved: list[str] = []
            message = f"Merged {effective_source} into local {merge_target}"

            if merge_result.returncode != 0:
                # Detect unmerged (conflicted) files via git index — more reliable
                # than parsing human-readable merge output for "CONFLICT" strings
                conflicted_files = await asyncio.to_thread(
                    resolved_git_mgr.get_unmerged_files, cwd=merge_cwd
                )
                if conflicted_files:
                    # Auto-resolve trivial conflicts (.gobby/*.jsonl)
                    from gobby.worktrees.merge.resolver import auto_resolve_trivial_conflicts

                    remaining = await auto_resolve_trivial_conflicts(conflicted_files, merge_cwd)

                    if not remaining:
                        # All conflicts were trivial — commit the merge and continue
                        commit_result = await asyncio.to_thread(
                            resolved_git_mgr.run_git_command,
                            ["commit", "--no-edit"],
                            cwd=merge_cwd,
                            timeout=30,
                        )
                        if commit_result.returncode != 0:
                            return {
                                "success": False,
                                "error": (
                                    f"Commit after trivial conflict resolution failed: "
                                    f"{(commit_result.stderr or commit_result.stdout).strip()}"
                                ),
                                "worktree_path": wt_path,
                                "project_path": repo_path,
                                "target_worktree_path": target_worktree_path,
                                "source_branch": effective_source,
                                "target_branch": merge_target,
                            }
                        auto_resolved = conflicted_files
                        message = (
                            f"Merged {effective_source} into local {merge_target} "
                            f"(auto-resolved {len(conflicted_files)} trivial conflict(s))"
                        )
                    else:
                        # Still have real conflicts — abort and report.
                        await asyncio.to_thread(
                            resolved_git_mgr.run_git_command,
                            ["merge", "--abort"],
                            cwd=merge_cwd,
                            timeout=10,
                        )
                        return {
                            "success": False,
                            "has_conflicts": True,
                            "merged": False,
                            "conflicted_files": remaining,
                            "auto_resolved": [f for f in conflicted_files if f not in remaining],
                            "worktree_path": wt_path,
                            "project_path": repo_path,
                            "target_worktree_path": target_worktree_path,
                            "message": (
                                f"Merge conflicts detected in {len(remaining)} file(s) "
                                f"({len(conflicted_files) - len(remaining)} trivial auto-resolved). "
                                "Use gobby-merge tools to resolve."
                            ),
                        }

                else:
                    merge_output = merge_result.stdout + merge_result.stderr
                    await asyncio.to_thread(
                        resolved_git_mgr.run_git_command,
                        ["merge", "--abort"],
                        cwd=merge_cwd,
                        timeout=10,
                    )
                    return {
                        "success": False,
                        "has_conflicts": False,
                        "worktree_path": wt_path,
                        "project_path": repo_path,
                        "target_worktree_path": target_worktree_path,
                        "error": merge_output.strip(),
                    }

            git_merged = await _source_is_merged_into_target()
            if git_merged:
                ctx.worktree_storage.mark_merged(worktree_id)
                target_sha_result = await asyncio.to_thread(
                    resolved_git_mgr.run_git_command,
                    ["rev-parse", "HEAD"],
                    cwd=merge_cwd,
                    timeout=10,
                )
                if target_sha_result.returncode != 0:
                    return {
                        "success": False,
                        "error": (
                            "Merged local target branch, but failed to determine final "
                            f"target SHA: {target_sha_result.stderr.strip()}"
                        ),
                        "worktree_path": wt_path,
                        "project_path": repo_path,
                        "target_worktree_path": target_worktree_path,
                        "source_branch": effective_source,
                        "target_branch": merge_target,
                        "merged": True,
                        "pushed": False,
                    }
                target_head_sha = target_sha_result.stdout.strip()
            else:
                target_head_sha = None

            result = {
                "success": True,
                "message": message if git_merged else "Local target branch was not updated",
                "worktree_path": wt_path,
                "project_path": repo_path,
                "target_worktree_path": target_worktree_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
                "merged": git_merged,
                "pushed": False,
            }
            if target_head_sha:
                result["merge_sha"] = target_head_sha
                result["target_head_sha"] = target_head_sha
                result["commit_sha"] = target_head_sha
            if auto_resolved:
                result["auto_resolved"] = auto_resolved
            return result
        finally:
            if checked_out_target and original_branch != merge_target:
                restore_branch = await asyncio.to_thread(
                    resolved_git_mgr.run_git_command,
                    ["checkout", original_branch],
                    cwd=merge_cwd,
                    timeout=30,
                )
                if restore_branch.returncode != 0:
                    logger.warning(
                        "Failed to restore original branch %s after merge_worktree: %s",
                        original_branch,
                        restore_branch.stderr or restore_branch.stdout,
                    )
            await _restore_stash()

    @registry.tool(
        name="push_branch",
        description="Push a worktree branch to a remote branch.",
    )
    async def push_branch(
        worktree_id: str,
        branch: str | None = None,
        remote: str = "origin",
        target_branch: str | None = None,
        force_with_lease: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Push a branch from the isolated worktree."""
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

        source_branch = branch or worktree.branch_name
        destination_branch = target_branch or source_branch
        command = ["push", "--no-verify"]
        if force_with_lease:
            command.append("--force-with-lease")
        command.extend([remote, f"{source_branch}:{destination_branch}"])

        result = await asyncio.to_thread(
            resolved_git_mgr.run_git_command,
            command,
            cwd=worktree.worktree_path,
            timeout=60,
        )
        return {
            "success": result.returncode == 0,
            "worktree_id": worktree_id,
            "worktree_path": worktree.worktree_path,
            "branch": source_branch,
            "remote": remote,
            "target_branch": destination_branch,
            "force_with_lease": force_with_lease,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": None if result.returncode == 0 else result.stderr.strip(),
        }

    return registry
