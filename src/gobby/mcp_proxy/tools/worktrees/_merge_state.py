"""Git-backed worktree merge-state helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.storage.worktrees import Worktree, WorktreeStatus

if TYPE_CHECKING:
    from gobby.worktrees.git import WorktreeGitManager


def _branch_candidates(branch: str) -> tuple[str, ...]:
    if branch.startswith("origin/"):
        return (branch,)
    return (branch, f"origin/{branch}")


def _git_cwd(worktree: Worktree, git_manager: WorktreeGitManager) -> str:
    if Path(worktree.worktree_path).exists():
        return worktree.worktree_path
    return str(git_manager.repo_path)


def is_branch_ancestor(
    git_manager: WorktreeGitManager,
    source_branch: str,
    target_branch: str,
    *,
    cwd: str,
) -> bool:
    """Return whether source_branch is an ancestor of target_branch in git."""
    for source_ref in _branch_candidates(source_branch):
        for target_ref in _branch_candidates(target_branch):
            result = git_manager._run_git(
                ["merge-base", "--is-ancestor", source_ref, target_ref],
                cwd=cwd,
                timeout=10,
            )
            if result.returncode == 0:
                return True
    return False


def is_worktree_git_merged(
    worktree: Worktree,
    git_manager: WorktreeGitManager | None,
) -> bool | None:
    """Return git ancestry merge state, or None when git is unavailable."""
    if git_manager is None:
        return None
    return is_branch_ancestor(
        git_manager,
        worktree.branch_name,
        worktree.base_branch,
        cwd=_git_cwd(worktree, git_manager),
    )


def merge_state_payload(
    worktree: Worktree,
    git_manager: WorktreeGitManager | None,
) -> dict[str, Any]:
    """Build merge-state details for API responses."""
    git_merged = is_worktree_git_merged(worktree, git_manager)
    return {
        "source_branch": worktree.branch_name,
        "target_branch": worktree.base_branch,
        "stored_status": worktree.status,
        "git_merged": git_merged,
        "consistent": worktree.status != WorktreeStatus.MERGED.value or git_merged is not False,
    }


def worktree_dict_with_git_merge_state(
    worktree: Worktree,
    git_manager: WorktreeGitManager | None,
) -> dict[str, Any]:
    """Return worktree dict with stale merged metadata corrected for responses."""
    data = worktree.to_dict()
    state = merge_state_payload(worktree, git_manager)
    data["git_merge_state"] = state

    if worktree.status == WorktreeStatus.MERGED.value and state["git_merged"] is False:
        data["stored_status"] = worktree.status
        data["status"] = WorktreeStatus.ACTIVE.value
        data["merged_at"] = None
        data["cleanup_after"] = None

    return data
