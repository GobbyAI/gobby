"""Worktree sync and merge tools."""

from __future__ import annotations

import asyncio
import logging

# Used only to classify timeout exceptions from the existing Git runner.
import subprocess  # nosec
from typing import Any, Literal, cast

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.mcp_proxy.tools.worktrees._helpers import resolve_project_context
from gobby.mcp_proxy.tools.worktrees._merge_fallback import (
    _non_gobby_dirty_paths,
    _non_gobby_status_lines,
    land_by_fast_forward,
    staged_paths,
)
from gobby.utils.git import (
    get_checkout_mutation_lock,
    new_stash_marker,
    run_thread_to_completion,
    run_to_completion,
    stash_oid_for_marker,
    stash_ref_for_oid,
)

logger = logging.getLogger(__name__)

MERGE_COMMAND_TIMEOUT_SECONDS = 240


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
        source_branch: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Sync a worktree with the main branch.

        Args:
            worktree_id: The worktree ID to sync.
            strategy: Sync strategy ('merge' or 'rebase').
            source_branch: Explicit source branch/ref to sync from. Defaults to the
                worktree's stored base_branch.
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
        if worktree.branch_name is None:
            return {
                "success": False,
                "error": f"Detached worktree '{worktree_id}' cannot be synced",
            }

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
            source_branch=source_branch,
        )

        if not result.success:
            return {"success": False, "error": result.error or "Sync failed"}

        ctx.worktree_storage.touch(worktree_id)

        return {
            "success": True,
            "message": result.message,
            "output": result.output,
            "strategy": strategy,
            "source_branch": source_branch or worktree.base_branch,
        }

    async def _merge_worktree_impl(
        worktree_id: str,
        source_branch: str | None = None,
        target_branch: str | None = None,
        push: bool = False,
        prefer_remote: bool = False,
        project_path: str | None = None,
        cancellation_requested: asyncio.Event | None = None,
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
        if worktree.branch_name is None:
            return {
                "success": False,
                "error": f"Detached worktree '{worktree_id}' cannot be merged",
            }

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
        source_ref = f"refs/heads/{effective_source}"
        target_ref = f"refs/heads/{merge_target}"
        wt_path = worktree.worktree_path
        repo_path = str(resolved_git_mgr.repo_path)
        target_worktree_path = await asyncio.to_thread(
            _worktree_path_for_branch, resolved_git_mgr, merge_target
        )
        merge_cwd = target_worktree_path or repo_path

        target_ref_result = await asyncio.to_thread(
            resolved_git_mgr.run_git_command,
            ["show-ref", "--verify", "--quiet", target_ref],
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
            ["show-ref", "--verify", "--quiet", source_ref],
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

        original_branch = ""
        checked_out_target = False
        merge_cleanup_required = False

        stash_oid: str | None = None

        async def _restore_stash() -> None:
            """Restore stashed .gobby/ files if any were stashed."""
            if stash_oid:
                stash_list = await run_thread_to_completion(
                    resolved_git_mgr.run_git_command,
                    ["stash", "list", "--format=%gd%x00%H"],
                    cwd=merge_cwd,
                    timeout=10,
                )
                if stash_list.returncode != 0:
                    detail = stash_list.stderr or stash_list.stdout or "git stash list failed"
                    raise RuntimeError(f"Failed to locate merge_worktree stash: {detail}")
                stash_ref = stash_ref_for_oid(stash_list.stdout, stash_oid)
                if stash_ref is None:
                    raise RuntimeError(f"Failed to locate exact merge_worktree stash {stash_oid}")
                pop_result = await run_thread_to_completion(
                    resolved_git_mgr.run_git_command,
                    ["stash", "pop", stash_ref],
                    cwd=merge_cwd,
                    timeout=10,
                )
                if pop_result.returncode != 0:
                    detail = pop_result.stderr or pop_result.stdout or "git stash pop failed"
                    raise RuntimeError(
                        f"Failed to restore stashed .gobby/ files from {stash_ref}: {detail}"
                    )

        async def _abort_failed_merge() -> None:
            """Abort a failed merge transaction if Git still has MERGE_HEAD."""
            nonlocal merge_cleanup_required
            if not merge_cleanup_required:
                return

            try:
                merge_head = await run_thread_to_completion(
                    resolved_git_mgr.run_git_command,
                    ["rev-parse", "--verify", "-q", "MERGE_HEAD"],
                    cwd=merge_cwd,
                    timeout=10,
                )
            except (subprocess.TimeoutExpired, OSError) as error:
                raise RuntimeError(f"Failed to inspect failed merge state: {error}") from error

            if merge_head.returncode == 1:
                merge_cleanup_required = False
                return
            if merge_head.returncode != 0:
                detail = (
                    merge_head.stderr
                    or merge_head.stdout
                    or f"git exited with status {merge_head.returncode}"
                )
                raise RuntimeError(f"Failed to inspect failed merge state: {detail}")

            try:
                abort_result = await run_thread_to_completion(
                    resolved_git_mgr.run_git_command,
                    ["merge", "--abort"],
                    cwd=merge_cwd,
                    timeout=10,
                )
            except (subprocess.TimeoutExpired, OSError) as error:
                raise RuntimeError(f"Failed to abort merge_worktree merge: {error}") from error
            if abort_result.returncode != 0:
                detail = (
                    abort_result.stderr
                    or abort_result.stdout
                    or f"git exited with status {abort_result.returncode}"
                )
                raise RuntimeError(f"Failed to abort merge_worktree merge: {detail}")
            merge_cleanup_required = False

        async def _source_is_merged_into_target() -> bool:
            ancestor_result = await run_thread_to_completion(
                resolved_git_mgr.run_git_command,
                [
                    "merge-base",
                    "--is-ancestor",
                    source_ref,
                    target_ref,
                ],
                cwd=merge_cwd,
                timeout=10,
            )
            return ancestor_result.returncode == 0

        async def _worktree_branch_is_merged_into_base(
            effective_merge_result: bool,
        ) -> bool:
            if effective_source == worktree.branch_name and merge_target == worktree.base_branch:
                return effective_merge_result
            ancestor_result = await run_thread_to_completion(
                resolved_git_mgr.run_git_command,
                [
                    "merge-base",
                    "--is-ancestor",
                    f"refs/heads/{worktree.branch_name}",
                    f"refs/heads/{worktree.base_branch}",
                ],
                cwd=merge_cwd,
                timeout=10,
            )
            return ancestor_result.returncode == 0

        if worktree.status == "merged" and await _source_is_merged_into_target():
            target_sha_result = await asyncio.to_thread(
                resolved_git_mgr.run_git_command,
                ["rev-parse", target_ref],
                cwd=merge_cwd,
                timeout=10,
            )
            if target_sha_result.returncode != 0:
                return {
                    "success": False,
                    "error": (
                        "Source is already merged into the local target branch, but failed "
                        f"to determine target SHA: {target_sha_result.stderr.strip()}"
                    ),
                    "worktree_path": wt_path,
                    "project_path": repo_path,
                    "target_worktree_path": target_worktree_path,
                    "source_branch": effective_source,
                    "target_branch": merge_target,
                    "merged": True,
                    "pushed": False,
                }
            reconciled_target_sha = target_sha_result.stdout.strip()
            if await _worktree_branch_is_merged_into_base(True):
                ctx.worktree_storage.mark_merged(worktree_id)
            return {
                "success": True,
                "message": (
                    f"{effective_source} is already merged into local {merge_target}; "
                    "reconciled completed merge"
                ),
                "worktree_path": wt_path,
                "project_path": repo_path,
                "target_worktree_path": target_worktree_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
                "merged": True,
                "reconciled": True,
                "pushed": False,
                "merge_sha": reconciled_target_sha,
                "target_head_sha": reconciled_target_sha,
                "commit_sha": reconciled_target_sha,
            }

        mutation_lock = get_checkout_mutation_lock(merge_cwd)
        await mutation_lock.acquire()
        try:
            if cancellation_requested is not None and cancellation_requested.is_set():
                raise asyncio.CancelledError
            current_branch_result = await run_thread_to_completion(
                resolved_git_mgr.run_git_command,
                ["rev-parse", "--abbrev-ref", "HEAD"],
                cwd=merge_cwd,
                timeout=10,
            )
            if current_branch_result.returncode != 0:
                return {
                    "success": False,
                    "error": (
                        "Failed to determine current branch: "
                        f"{current_branch_result.stderr.strip()}"
                    ),
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
            if original_branch != merge_target:
                checkout_result = await run_thread_to_completion(
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

            status_result = await run_thread_to_completion(
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
            try:
                target_staged_paths = await asyncio.to_thread(
                    staged_paths,
                    resolved_git_mgr,
                    merge_cwd,
                )
            except RuntimeError as error:
                return {
                    "success": False,
                    "error": str(error),
                    "step": "inspect-staged",
                    "worktree_path": wt_path,
                    "project_path": repo_path,
                    "target_worktree_path": target_worktree_path,
                    "source_branch": effective_source,
                    "target_branch": merge_target,
                }
            dirty_paths = _non_gobby_dirty_paths(status_result.stdout) | target_staged_paths
            if dirty_paths:
                incoming_result = await run_thread_to_completion(
                    resolved_git_mgr.run_git_command,
                    ["diff", "--name-only", "HEAD", source_ref],
                    cwd=merge_cwd,
                    timeout=10,
                )
                if incoming_result.returncode != 0:
                    return {
                        "success": False,
                        "error": (
                            "Failed to inspect incoming merge paths: "
                            f"{incoming_result.stderr.strip()}"
                        ),
                        "worktree_path": wt_path,
                        "project_path": repo_path,
                        "target_worktree_path": target_worktree_path,
                        "source_branch": effective_source,
                        "target_branch": merge_target,
                    }
                incoming_paths = {
                    line.strip() for line in incoming_result.stdout.splitlines() if line.strip()
                }
                overlapping = sorted(dirty_paths & incoming_paths)
                if overlapping:
                    return {
                        "success": False,
                        "error": "Target checkout has uncommitted changes that overlap merge",
                        "dirty_files": _non_gobby_status_lines(status_result.stdout),
                        "overlapping_dirty_paths": overlapping,
                        "worktree_path": wt_path,
                        "project_path": repo_path,
                        "target_worktree_path": target_worktree_path,
                        "source_branch": effective_source,
                        "target_branch": merge_target,
                    }

            # Stash dirty .gobby/ sync files and retain the exact object identity.
            stash_marker = new_stash_marker("merge-worktree")
            stash_head_before = await run_thread_to_completion(
                resolved_git_mgr.run_git_command,
                ["stash", "list", "-1", "--format=%H"],
                cwd=merge_cwd,
                timeout=10,
            )
            if stash_head_before.returncode != 0:
                detail = (
                    stash_head_before.stderr
                    or stash_head_before.stdout
                    or "git stash identity lookup failed"
                )
                return {
                    "success": False,
                    "error": f"Failed to inspect target checkout stash identity: {detail}",
                    "step": "stash",
                    "worktree_path": wt_path,
                    "project_path": repo_path,
                    "target_worktree_path": target_worktree_path,
                    "source_branch": effective_source,
                    "target_branch": merge_target,
                }
            stash_push = await run_thread_to_completion(
                resolved_git_mgr.run_git_command,
                ["stash", "push", "-m", stash_marker, "--", ".gobby/"],
                cwd=merge_cwd,
                timeout=10,
            )
            if stash_push.returncode != 0:
                detail = stash_push.stderr or stash_push.stdout or "git stash push failed"
                return {
                    "success": False,
                    "error": f"Failed to stash target checkout .gobby files: {detail}",
                    "step": "stash",
                    "worktree_path": wt_path,
                    "project_path": repo_path,
                    "target_worktree_path": target_worktree_path,
                    "source_branch": effective_source,
                    "target_branch": merge_target,
                }
            stash_head_after = await run_thread_to_completion(
                resolved_git_mgr.run_git_command,
                ["stash", "list", "--format=%H%x00%gs"],
                cwd=merge_cwd,
                timeout=10,
            )
            if stash_head_after.returncode != 0:
                detail = (
                    stash_head_after.stderr
                    or stash_head_after.stdout
                    or "git stash identity lookup failed"
                )
                return {
                    "success": False,
                    "error": f"Failed to identify target checkout stash: {detail}",
                    "step": "stash",
                    "worktree_path": wt_path,
                    "project_path": repo_path,
                    "target_worktree_path": target_worktree_path,
                    "source_branch": effective_source,
                    "target_branch": merge_target,
                }
            before_oid = stash_head_before.stdout.strip() or None
            after_oid = stash_head_after.stdout.partition("\0")[0].strip() or None
            stash_oid = stash_oid_for_marker(stash_head_after.stdout, stash_marker)
            if stash_oid is None and after_oid != before_oid:
                return {
                    "success": False,
                    "error": (
                        "Stash head changed after push but the operation-owned "
                        "stash marker was not found"
                    ),
                    "step": "stash",
                    "worktree_path": wt_path,
                    "project_path": repo_path,
                    "target_worktree_path": target_worktree_path,
                    "source_branch": effective_source,
                    "target_branch": merge_target,
                }

            merge_head_result = await run_thread_to_completion(
                resolved_git_mgr.run_git_command,
                ["rev-parse", "--abbrev-ref", "HEAD"],
                cwd=merge_cwd,
                timeout=10,
            )
            if (
                merge_head_result.returncode != 0
                or merge_head_result.stdout.strip() != merge_target
            ):
                observed_branch = merge_head_result.stdout.strip() or "unknown"
                return {
                    "success": False,
                    "error": (
                        f"Target checkout moved to '{observed_branch}' before merge; "
                        f"expected '{merge_target}'"
                    ),
                    "worktree_path": wt_path,
                    "project_path": repo_path,
                    "target_worktree_path": target_worktree_path,
                    "source_branch": effective_source,
                    "target_branch": merge_target,
                }

            landing = "merge"
            if target_staged_paths:
                fallback_result = await asyncio.to_thread(
                    land_by_fast_forward,
                    resolved_git_mgr,
                    source_cwd=wt_path,
                    target_cwd=merge_cwd,
                    source_ref=source_ref,
                    target_ref=target_ref,
                )
                if not fallback_result.success:
                    conflicted_files = list(fallback_result.conflicted_files)
                    if conflicted_files:
                        return {
                            "success": False,
                            "has_conflicts": True,
                            "merged": False,
                            "conflicted_files": conflicted_files,
                            "step": fallback_result.step,
                            "worktree_path": wt_path,
                            "project_path": repo_path,
                            "target_worktree_path": target_worktree_path,
                            "source_branch": effective_source,
                            "target_branch": merge_target,
                            "message": (
                                f"Merge conflicts detected in {len(conflicted_files)} file(s). "
                                "Use gobby-merge tools to resolve."
                            ),
                        }
                    return {
                        "success": False,
                        "has_conflicts": False,
                        "worktree_path": wt_path,
                        "project_path": repo_path,
                        "target_worktree_path": target_worktree_path,
                        "source_branch": effective_source,
                        "target_branch": merge_target,
                        "step": fallback_result.step,
                        "error": fallback_result.error or "Fast-forward landing failed",
                    }
                landing = "fast-forward"
            else:
                # Git can leave MERGE_HEAD/index state behind even when the command
                # raises instead of returning a nonzero result (for example, timeout).
                # Treat the transaction as cleanup-required before starting merge and
                # clear the flag only after Git proves the merge command succeeded.
                merge_cleanup_required = True
                merge_result = await run_thread_to_completion(
                    resolved_git_mgr.run_git_command,
                    ["merge", source_ref, "--no-ff", "--no-edit"],
                    cwd=merge_cwd,
                    timeout=MERGE_COMMAND_TIMEOUT_SECONDS,
                    env={"GOBBY_MERGE": "1"},
                )
                if merge_result.returncode == 0:
                    merge_cleanup_required = False
                if merge_result.returncode != 0:
                    # Detect unmerged (conflicted) files via git index — more reliable
                    # than parsing human-readable merge output for "CONFLICT" strings
                    conflicted_files = await run_thread_to_completion(
                        resolved_git_mgr.get_unmerged_files, cwd=merge_cwd
                    )
                    if conflicted_files:
                        # The transaction cleanup below aborts before the checkout
                        # lock is released.
                        return {
                            "success": False,
                            "has_conflicts": True,
                            "merged": False,
                            "conflicted_files": conflicted_files,
                            "worktree_path": wt_path,
                            "project_path": repo_path,
                            "target_worktree_path": target_worktree_path,
                            "message": (
                                f"Merge conflicts detected in {len(conflicted_files)} file(s). "
                                "Use gobby-merge tools to resolve."
                            ),
                        }

                    merge_output = "\n".join(
                        output.strip()
                        for output in (merge_result.stdout, merge_result.stderr)
                        if output.strip()
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
                if await _worktree_branch_is_merged_into_base(git_merged):
                    ctx.worktree_storage.mark_merged(worktree_id)
                target_sha_result = await run_thread_to_completion(
                    resolved_git_mgr.run_git_command,
                    ["rev-parse", target_ref],
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
                "message": (
                    f"Merged {effective_source} into local {merge_target}"
                    if git_merged
                    else "Local target branch was not updated"
                ),
                "worktree_path": wt_path,
                "project_path": repo_path,
                "target_worktree_path": target_worktree_path,
                "source_branch": effective_source,
                "target_branch": merge_target,
                "landing": landing,
                "merged": git_merged,
                "pushed": False,
            }
            if target_head_sha:
                result["merge_sha"] = target_head_sha
                result["target_head_sha"] = target_head_sha
                result["commit_sha"] = target_head_sha
            return result
        finally:
            cleanup_errors: list[RuntimeError] = []
            try:
                try:
                    await _abort_failed_merge()
                except RuntimeError as abort_error:
                    cleanup_errors.append(abort_error)
                    logger.error("%s", abort_error)
                if checked_out_target and original_branch != merge_target:
                    restore_branch = await run_thread_to_completion(
                        resolved_git_mgr.run_git_command,
                        ["checkout", original_branch],
                        cwd=merge_cwd,
                        timeout=30,
                    )
                    if restore_branch.returncode != 0:
                        detail = (
                            restore_branch.stderr
                            or restore_branch.stdout
                            or f"git exited with status {restore_branch.returncode}"
                        )
                        cleanup_errors.append(
                            RuntimeError(
                                f"Failed to restore original branch {original_branch} "
                                f"after merge_worktree: {detail}"
                            )
                        )
                        logger.error("%s", cleanup_errors[-1])
                try:
                    await _restore_stash()
                except RuntimeError as stash_error:
                    cleanup_errors.append(stash_error)
                    logger.error("%s", stash_error)
                if cleanup_errors:
                    raise RuntimeError("; ".join(str(error) for error in cleanup_errors))
            finally:
                mutation_lock.release()

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
        cancellation_requested = asyncio.Event()
        return await run_to_completion(
            _merge_worktree_impl(
                worktree_id,
                source_branch,
                target_branch,
                push,
                prefer_remote,
                project_path,
                cancellation_requested,
            ),
            on_cancel=cancellation_requested.set,
        )

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
        if worktree.branch_name is None:
            return {
                "success": False,
                "error": f"Detached worktree '{worktree_id}' cannot be pushed",
            }

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
