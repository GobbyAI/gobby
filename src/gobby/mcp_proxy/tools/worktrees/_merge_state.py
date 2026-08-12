"""Git-backed worktree merge-state helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.storage.worktrees import Worktree, WorktreeStatus

if TYPE_CHECKING:
    from gobby.worktrees.git import WorktreeGitManager


def _qualified_ref(branch: str) -> str:
    """Fully qualify a branch name so short-ref ambiguity cannot leak in."""
    if branch.startswith("refs/"):
        return branch
    if branch.startswith("origin/"):
        return f"refs/remotes/{branch}"
    return f"refs/heads/{branch}"


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
    """Return whether source_branch is an ancestor of target_branch in git.

    Exactly one fully qualified check. No origin/* fallback: a stale-but-merged
    remote ref must not report a diverged local branch as merged.
    """
    result = git_manager.run_git_command(
        [
            "merge-base",
            "--is-ancestor",
            _qualified_ref(source_branch),
            _qualified_ref(target_branch),
        ],
        cwd=cwd,
        timeout=10,
    )
    return result.returncode == 0


def is_worktree_git_merged(
    worktree: Worktree,
    git_manager: WorktreeGitManager | None,
) -> bool | None:
    """Return git ancestry merge state, or None when git is unavailable."""
    if git_manager is None or worktree.branch_name is None:
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
