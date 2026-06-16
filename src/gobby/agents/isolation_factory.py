"""Factory for selecting an agent isolation handler."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from gobby.agents.isolation_clone import CloneIsolationHandler
from gobby.agents.isolation_models import IsolationHandler
from gobby.agents.isolation_none import NoneIsolationHandler
from gobby.agents.isolation_worktree import WorktreeIsolationHandler

if TYPE_CHECKING:
    from gobby.clones.git import CloneGitManager
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.worktrees.git import WorktreeGitManager


def get_isolation_handler(
    mode: Literal["none", "worktree", "clone"],
    *,
    git_manager: WorktreeGitManager | None = None,
    worktree_storage: LocalWorktreeManager | None = None,
    clone_manager: CloneGitManager | None = None,
    clone_storage: LocalCloneManager | None = None,
) -> IsolationHandler:
    """
    Factory function to get the appropriate isolation handler.

    Args:
        mode: Isolation mode - 'none', 'worktree', or 'clone'
        git_manager: Git manager for worktree operations (required for 'worktree')
        worktree_storage: Storage for worktree records (required for 'worktree')
        clone_manager: Git manager for clone operations (required for 'clone')
        clone_storage: Storage for clone records (required for 'clone')

    Returns:
        IsolationHandler instance for the specified mode

    Raises:
        ValueError: If mode is unknown or required dependencies are missing
    """
    if mode == "none":
        return NoneIsolationHandler()

    if mode == "worktree":
        if git_manager is None or worktree_storage is None:
            raise ValueError("git_manager and worktree_storage are required for worktree isolation")
        return WorktreeIsolationHandler(
            git_manager=git_manager,
            worktree_storage=worktree_storage,
        )

    if mode == "clone":
        if clone_manager is None or clone_storage is None:
            raise ValueError("clone_manager and clone_storage are required for clone isolation")
        return CloneIsolationHandler(
            clone_manager=clone_manager,
            clone_storage=clone_storage,
            git_manager=git_manager,  # For branch detection
        )

    raise ValueError(f"Unknown isolation mode: {mode}")
