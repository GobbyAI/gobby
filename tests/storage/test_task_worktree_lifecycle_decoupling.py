"""Regression tests for decoupled task/worktree lifecycle state."""

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager, WorktreeStatus


def test_close_task_does_not_mark_linked_worktree_merged(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    worktree_manager = LocalWorktreeManager(temp_db)
    task = task_manager.create_task(project_id=sample_project["id"], title="Close linked task")
    worktree = worktree_manager.create(
        project_id=sample_project["id"],
        branch_name="feature/close-linked-task",
        worktree_path="/tmp/gobby-close-linked-task",
        task_id=task.id,
    )

    task_manager.close_task(task.id, reason="completed")

    refreshed = worktree_manager.get(worktree.id)
    assert refreshed is not None
    assert refreshed.status == WorktreeStatus.ACTIVE.value
    assert refreshed.merged_at is None
    assert refreshed.cleanup_after is None


@pytest.mark.parametrize(
    ("transition_method", "expected_status"),
    [
        ("mark_merged", WorktreeStatus.MERGED.value),
        ("mark_abandoned", WorktreeStatus.ABANDONED.value),
    ],
)
def test_reopen_task_does_not_reactivate_linked_worktree(
    temp_db,
    sample_project,
    transition_method: str,
    expected_status: str,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    worktree_manager = LocalWorktreeManager(temp_db)
    task = task_manager.create_task(project_id=sample_project["id"], title="Reopen linked task")
    worktree = worktree_manager.create(
        project_id=sample_project["id"],
        branch_name=f"feature/reopen-linked-task-{expected_status}",
        worktree_path=f"/tmp/gobby-reopen-linked-task-{expected_status}",
        task_id=task.id,
    )
    task_manager.close_task(task.id, reason="completed")

    transitioned = getattr(worktree_manager, transition_method)(worktree.id)
    assert transitioned is not None

    task_manager.reopen_task(task.id)

    refreshed = worktree_manager.get(worktree.id)
    assert refreshed is not None
    assert refreshed.status == expected_status
    assert refreshed.merged_at == transitioned.merged_at
    assert refreshed.cleanup_after == transitioned.cleanup_after
