"""Git worktree operations manager (subpackage)."""

from gobby.worktrees.git._branch import BranchDivergenceUnavailableError, get_default_branch
from gobby.worktrees.git._models import GitOperationResult, WorktreeInfo, WorktreeStatus
from gobby.worktrees.git.manager import WorktreeGitManager

__all__ = [
    "BranchDivergenceUnavailableError",
    "GitOperationResult",
    "WorktreeGitManager",
    "WorktreeInfo",
    "WorktreeStatus",
    "get_default_branch",
]
