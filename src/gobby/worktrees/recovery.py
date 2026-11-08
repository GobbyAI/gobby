"""Startup pass that finishes worktree deletions a shutdown interrupted (#25838).

A deletion marks its record ``deleting`` before its first destructive step. A record still
marked when the next start runs was interrupted; this pass re-runs its deletion on the
RECOVERY surface, which admits only tracked deletions left on disk. Stateless and idempotent:
the marked rows are the whole input.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from gobby.storage.worktrees import WorktreeStatus
from gobby.worktrees.deletion import (
    DeletionSurface,
    GitManagerResolver,
    WorktreeDeletionRequest,
    WorktreeDeletionResult,
    delete_worktree_transaction,
)
from gobby.worktrees.executor import run_worktree_delete

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
    """Finish every worktree record left in the deleting state; returns one result per attempt.

    A refused record keeps its marker and its tree. A row another path finishes meanwhile is
    no longer listed or is refused as ineligible, so a repeat pass is a no-op.
    """
    rows = await asyncio.to_thread(
        worktree_storage.list_worktrees, status=WorktreeStatus.DELETING.value, limit=None
    )
    results: list[WorktreeDeletionResult] = []
    for row in rows:
        if should_stop():
            break
        request = WorktreeDeletionRequest(
            row.id, DeletionSurface.RECOVERY, merged_into=row.base_branch
        )
        results.append(
            await run_worktree_delete(
                executor,
                partial(
                    delete_worktree_transaction,
                    request=request,
                    worktree_storage=worktree_storage,
                    resolve_git_manager=resolve_git_manager,
                    task_manager=task_manager,
                ),
            )
        )
    return results
