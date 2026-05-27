"""Red tests for task artifact storage constraints and helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gobby.storage.tasks import (
    LocalTaskManager,
    TaskArtifactConstraintError,
    TaskArtifactManager,
)

pytestmark = pytest.mark.unit


def test_set_artifacts_atomic_enforces_worktree_pair_copresence(
    temp_db,
    sample_project,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifacts",
    )
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(TaskArtifactConstraintError) as exc_info:
        manager.set_artifacts_atomic(task.id, worktree_path="/tmp/gobby-wt")

    assert exc_info.value.predicate == "worktree_pair"


def test_set_artifacts_atomic_enforces_clone_pair_copresence(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifacts",
    )
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(TaskArtifactConstraintError) as exc_info:
        manager.set_artifacts_atomic(task.id, clone_id="clone-row-1")

    assert exc_info.value.predicate == "clone_pair"


def test_set_artifacts_atomic_enforces_isolation_family_xor(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifacts",
    )
    manager = TaskArtifactManager(temp_db)

    manager.set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/gobby-wt",
        worktree_id="worktree-row-1",
        base_commit_sha="abc123",
    )

    with pytest.raises(TaskArtifactConstraintError) as exc_info:
        manager.set_artifacts_atomic(
            task.id,
            clone_path="/tmp/gobby-clone",
            clone_id="clone-row-1",
            base_commit_sha="def456",
        )

    assert exc_info.value.predicate == "isolation_family_xor"


def test_clear_isolation_pair_atomically_clears_named_family(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Clear artifacts",
    )
    manager = TaskArtifactManager(temp_db)

    manager.set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/gobby-wt",
        worktree_id="worktree-row-1",
        base_commit_sha="abc123",
        target_branch="release/0.4",
    )
    manager.clear_isolation_pair(task.id, family="worktree")
    artifacts = manager.get_artifacts(task.id)

    assert artifacts.worktree_path is None
    assert artifacts.worktree_id is None
    assert artifacts.target_branch == "release/0.4"

    manager.set_artifacts_atomic(
        task.id,
        clone_path="/tmp/gobby-clone",
        clone_id="clone-row-1",
        base_commit_sha="def456",
    )
    artifacts = manager.get_artifacts(task.id)
    assert artifacts.clone_path == "/tmp/gobby-clone"
    assert artifacts.clone_id == "clone-row-1"


def test_clear_worktree_references_clears_integration_branch(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Clear integration worktree",
    )
    manager = TaskArtifactManager(temp_db)
    manager.set_artifacts_atomic(
        task.id,
        target_branch="main",
        integration_branch="gobby/integration/task",
        integration_workspace_id="wt-integration",
    )

    assert manager.clear_worktree_references("wt-integration") == 1

    artifacts = manager.get_artifacts(task.id)
    assert artifacts.integration_workspace_id is None
    assert artifacts.integration_branch is None
    assert artifacts.target_branch == "main"


def test_clear_worktree_references_rolls_back_partial_failure(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    task_a = task_manager.create_task(
        project_id=sample_project["id"],
        title="Rollback artifacts A",
    )
    task_b = task_manager.create_task(
        project_id=sample_project["id"],
        title="Rollback artifacts B",
    )
    manager = TaskArtifactManager(temp_db)
    for task, path in ((task_a, "/tmp/wt-a"), (task_b, "/tmp/wt-b")):
        manager.set_artifacts_atomic(
            task.id,
            worktree_path=path,
            worktree_id="wt-rollback",
            base_commit_sha="abc123",
        )

    original_set_artifacts_atomic = manager.set_artifacts_atomic
    calls: list[str] = []

    def fail_after_first_update(task_id: str, **fields: str | int | None):
        calls.append(task_id)
        if len(calls) == 2:
            raise RuntimeError("injected failure")
        return original_set_artifacts_atomic(task_id, **fields)

    with (
        patch.object(manager, "set_artifacts_atomic", side_effect=fail_after_first_update),
        pytest.raises(RuntimeError, match="injected failure"),
    ):
        manager.clear_worktree_references("wt-rollback")

    assert len(calls) == 2
    artifacts_a = manager.get_artifacts(task_a.id)
    artifacts_b = manager.get_artifacts(task_b.id)
    assert artifacts_a.worktree_id == "wt-rollback"
    assert artifacts_a.worktree_path == "/tmp/wt-a"
    assert artifacts_a.base_commit_sha == "abc123"
    assert artifacts_b.worktree_id == "wt-rollback"
    assert artifacts_b.worktree_path == "/tmp/wt-b"
    assert artifacts_b.base_commit_sha == "abc123"
