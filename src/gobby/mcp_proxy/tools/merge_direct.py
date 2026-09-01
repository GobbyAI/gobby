"""Direct git merge helpers for gobby-merge MCP tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gobby.mcp_proxy.tools.merge_git_state import rev_parse_head
from gobby.mcp_proxy.tools.merge_github_protection import git_output
from gobby.mcp_proxy.tools.worktrees._merge_fallback import _non_gobby_status_lines
from gobby.worktrees.git import WorktreeGitManager

logger = logging.getLogger("gobby.mcp_proxy.tools.merge")

_NO_FF_STRATEGIES = {"no-ff", "no_ff"}
_GIT_NO_FF_TIER = "git_no_ff"


def _strategy_requests_no_ff(strategy: str) -> bool:
    return strategy.strip().lower() in _NO_FF_STRATEGIES


async def _dirty_worktree_result(
    git_manager: WorktreeGitManager | None,
    wt_path: str,
    *,
    after_merge: bool = False,
) -> dict[str, Any] | None:
    if git_manager is None:
        return {"success": False, "error": "git_manager not configured for clean-tree check"}

    status_result = await asyncio.to_thread(
        git_manager.run_git_command,
        ["status", "--porcelain"],
        cwd=wt_path,
        timeout=10,
    )
    if status_result.returncode != 0:
        phase = "after merge commit" if after_merge else "before merge"
        return {
            "success": False,
            "error": f"git status failed {phase}: {git_output(status_result)}",
        }

    dirty_files = _non_gobby_status_lines(status_result.stdout)
    if not dirty_files:
        return None

    return {
        "success": False,
        "error": (
            "merge completed but worktree is dirty"
            if after_merge
            else "worktree is dirty; commit or stash changes before merging"
        ),
        "dirty_files": dirty_files,
    }


async def _complete_direct_merge(
    git_manager: WorktreeGitManager | None,
    resolution: Any,
    wt_path: str,
) -> dict[str, Any]:
    if git_manager is None:
        return {"success": False, "error": "git_manager not configured"}

    repo_path = str(getattr(git_manager, "repo_path", None) or wt_path)
    dirty_result = await _dirty_worktree_result(git_manager, repo_path)
    if dirty_result is not None:
        return dirty_result

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
    restore_args = ["checkout", original_branch]
    restore_description = f"branch {original_branch}"
    if original_branch == "HEAD":
        original_sha = await rev_parse_head(git_manager, repo_path)
        if not original_sha:
            return {
                "success": False,
                "error": "Failed to determine current commit for detached HEAD",
            }
        restore_args = ["checkout", "--detach", original_sha]
        restore_description = f"detached HEAD at {original_sha}"
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

        dirty_result = await _dirty_worktree_result(
            git_manager,
            repo_path,
            after_merge=True,
        )
        if dirty_result is not None:
            return {
                "success": True,
                "merge_sha": merge_sha,
                "commit_sha": merge_sha,
                "merge_strategy": strategy_name,
                "merge_output": (merge_result.stdout or merge_result.stderr or "").strip(),
                "warning": dirty_result["error"],
                "dirty_files": dirty_result.get("dirty_files", []),
            }

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
                restore_args,
                cwd=repo_path,
                timeout=30,
            )
            if restore_result.returncode != 0:
                logger.warning(
                    "Failed to restore %s after direct merge: %s",
                    restore_description,
                    git_output(restore_result),
                )


__all__ = [
    "_GIT_NO_FF_TIER",
    "_NO_FF_STRATEGIES",
    "_complete_direct_merge",
    "_dirty_worktree_result",
    "_non_gobby_status_lines",
    "_strategy_requests_no_ff",
]
