"""Startup pass that finishes worktree deletions interrupted after the destructive boundary (#25838).

Behavior-free shape for the RED batch: the signature is fixed here so the tests reach their
assertions; the behavior lands in the GREEN commit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from gobby.worktrees.deletion import GitManagerResolver, WorktreeDeletionResult

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager
    from gobby.worktrees.executor import WorktreeDeleteExecutor


async def recover_deleting_worktrees(
    *,
    worktree_storage: LocalWorktreeManager,
    resolve_git_manager: GitManagerResolver,
    task_manager: LocalTaskManager | None,
    executor: WorktreeDeleteExecutor | None,
    should_stop: Callable[[], bool],
) -> list[WorktreeDeletionResult]:
    """Finish every worktree record left in the deleting state; returns one result per attempt."""
    raise NotImplementedError("recover_deleting_worktrees is not implemented yet")
