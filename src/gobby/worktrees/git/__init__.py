"""Git worktree operations manager (subpackage)."""

from gobby.worktrees.git._branch import get_default_branch
from gobby.worktrees.git._models import GitOperationResult, WorktreeInfo, WorktreeStatus
from gobby.worktrees.git.manager import WorktreeGitManager

__all__ = [
    "GitOperationResult",
    "WorktreeGitManager",
    "WorktreeInfo",
    "WorktreeStatus",
    "get_default_branch",
]
