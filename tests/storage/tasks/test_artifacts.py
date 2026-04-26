"""Red tests for task artifact storage constraints and helpers."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def _artifact_symbols() -> tuple[type, type[Exception]]:
    import gobby.storage.tasks as task_module

    return task_module.TaskArtifactManager, task_module.TaskArtifactConstraintError


def test_set_artifacts_atomic_enforces_worktree_pair_copresence(
    temp_db,
    sample_project,
) -> None:
    TaskArtifactManager, TaskArtifactConstraintError = _artifact_symbols()
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifacts",
    )
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(TaskArtifactConstraintError) as exc_info:
        manager.set_artifacts_atomic(task.id, worktree_path="/tmp/gobby-wt")

    assert exc_info.value.predicate == "worktree_pair"


def test_set_artifacts_atomic_enforces_clone_pair_copresence(temp_db, sample_project) -> None:
    TaskArtifactManager, TaskArtifactConstraintError = _artifact_symbols()
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifacts",
    )
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(TaskArtifactConstraintError) as exc_info:
        manager.set_artifacts_atomic(task.id, clone_id="clone-row-1")

    assert exc_info.value.predicate == "clone_pair"


def test_set_artifacts_atomic_enforces_isolation_family_xor(temp_db, sample_project) -> None:
    TaskArtifactManager, TaskArtifactConstraintError = _artifact_symbols()
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifacts",
    )
    manager = TaskArtifactManager(temp_db)

    manager.set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/gobby-wt",
        worktree_id="worktree-row-1",
    )

    with pytest.raises(TaskArtifactConstraintError) as exc_info:
        manager.set_artifacts_atomic(
            task.id,
            clone_path="/tmp/gobby-clone",
            clone_id="clone-row-1",
        )

    assert exc_info.value.predicate == "isolation_family_xor"


def test_clear_isolation_pair_atomically_clears_named_family(temp_db, sample_project) -> None:
    TaskArtifactManager, _TaskArtifactConstraintError = _artifact_symbols()
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Clear artifacts",
    )
    manager = TaskArtifactManager(temp_db)

    manager.set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/gobby-wt",
        worktree_id="worktree-row-1",
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
    )
    artifacts = manager.get_artifacts(task.id)
    assert artifacts.clone_path == "/tmp/gobby-clone"
    assert artifacts.clone_id == "clone-row-1"
