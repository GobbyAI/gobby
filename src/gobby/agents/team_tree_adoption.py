"""Whether a pod may adopt the live tree its leaf already has, judged from the tree itself."""

from __future__ import annotations

from pathlib import Path

from gobby.agents.isolation_git_hygiene import blocking_status_lines
from gobby.storage.worktrees import Worktree
from gobby.utils.daemon_git import GitOk, daemon_git


async def adoption_refusal(
    tree: Worktree, *, base_branch: str, leaf_branch: str, main: Path
) -> str | None:
    """Why a pod cannot adopt ``tree`` as its own, or None when it is clean on its leaf branch."""
    if tree.branch_name != leaf_branch:
        return f"it is on {tree.branch_name}, not {leaf_branch}"
    if tree.base_branch != base_branch:
        return f"it is based on {tree.base_branch}, not {base_branch}"
    status = await daemon_git.run(("status", "--porcelain"), cwd=tree.worktree_path)
    if not isinstance(status, GitOk):
        return "its status could not be read"
    if blocking_status_lines(status.stdout, Path(tree.worktree_path), main):
        return "it has uncommitted changes"
    return None
