"""Red tests for task artifact storage constraints and helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import gobby.storage.tasks._artifacts as artifact_module
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
        validation_criteria="Test task completion is observable.",
    )
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(TaskArtifactConstraintError) as exc_info:
        manager.set_artifacts_atomic(task.id, worktree_path="/tmp/gobby-wt")

    assert exc_info.value.predicate == "worktree_pair"


def test_set_artifacts_atomic_enforces_clone_pair_copresence(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifacts",
        validation_criteria="Test task completion is observable.",
    )
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(TaskArtifactConstraintError) as exc_info:
        manager.set_artifacts_atomic(task.id, clone_id="cccccccc-cccc-4ccc-8ccc-cccccccccc01")

    assert exc_info.value.predicate == "clone_pair"


def test_set_artifacts_atomic_enforces_isolation_family_xor(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifacts",
        validation_criteria="Test task completion is observable.",
    )
    manager = TaskArtifactManager(temp_db)

    manager.set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/gobby-wt",
        worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee03",
        base_commit_sha="abc123",
    )

    with pytest.raises(TaskArtifactConstraintError) as exc_info:
        manager.set_artifacts_atomic(
            task.id,
            clone_path="/tmp/gobby-clone",
            clone_id="cccccccc-cccc-4ccc-8ccc-cccccccccc01",
            base_commit_sha="def456",
        )

    assert exc_info.value.predicate == "isolation_family_xor"


def test_clear_isolation_pair_atomically_clears_named_family(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Clear artifacts",
        validation_criteria="Test task completion is observable.",
    )
    manager = TaskArtifactManager(temp_db)

    manager.set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/gobby-wt",
        worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee03",
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
        clone_id="cccccccc-cccc-4ccc-8ccc-cccccccccc01",
        base_commit_sha="def456",
    )
    artifacts = manager.get_artifacts(task.id)
    assert artifacts.clone_path == "/tmp/gobby-clone"
    assert artifacts.clone_id == "cccccccc-cccc-4ccc-8ccc-cccccccccc01"


def test_clear_worktree_references_clears_integration_branch(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Clear integration worktree",
        validation_criteria="Test task completion is observable.",
    )
    manager = TaskArtifactManager(temp_db)
    manager.set_artifacts_atomic(
        task.id,
        target_branch="main",
        integration_branch="gobby/integration/task",
        integration_workspace_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01",
    )

    assert manager.clear_worktree_references("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee01") == 1

    artifacts = manager.get_artifacts(task.id)
    assert artifacts.integration_workspace_id is None
    assert artifacts.integration_branch is None
    assert artifacts.target_branch == "main"


def test_clear_worktree_references_rolls_back_partial_failure(temp_db, sample_project) -> None:
    task_manager = LocalTaskManager(temp_db)
    task_a = task_manager.create_task(
        project_id=sample_project["id"],
        title="Rollback artifacts A",
        validation_criteria="Test task completion is observable.",
    )
    task_b = task_manager.create_task(
        project_id=sample_project["id"],
        title="Rollback artifacts B",
        validation_criteria="Test task completion is observable.",
    )
    manager = TaskArtifactManager(temp_db)
    for task, path in ((task_a, "/tmp/wt-a"), (task_b, "/tmp/wt-b")):
        manager.set_artifacts_atomic(
            task.id,
            worktree_path=path,
            worktree_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeee02",
            base_commit_sha="abc123",
        )

    original_set_artifacts_atomic = artifact_module._set_artifacts_in_transaction
    calls: list[str] = []

    def fail_after_first_update(
        conn,
        task_id: str,
        fields: dict[str, str | int | None],
    ):
        calls.append(task_id)
        if len(calls) == 2:
            raise RuntimeError("injected failure")
        return original_set_artifacts_atomic(conn, task_id, fields)

    with (
        patch.object(
            artifact_module,
            "_set_artifacts_in_transaction",
            side_effect=fail_after_first_update,
        ),
        pytest.raises(RuntimeError, match="injected failure"),
    ):
        manager.clear_worktree_references("eeeeeeee-eeee-4eee-8eee-eeeeeeeeee02")

    assert len(calls) == 2
    artifacts_a = manager.get_artifacts(task_a.id)
    artifacts_b = manager.get_artifacts(task_b.id)
    assert artifacts_a.worktree_id == "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee02"
    assert artifacts_a.worktree_path == "/tmp/wt-a"
    assert artifacts_a.base_commit_sha == "abc123"
    assert artifacts_b.worktree_id == "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee02"
    assert artifacts_b.worktree_path == "/tmp/wt-b"
    assert artifacts_b.base_commit_sha == "abc123"


def test_plan_enhancement_fields_default_to_zero_and_false(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Enhancement defaults",
        validation_criteria="Test task completion is observable.",
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)

    assert artifacts.plan_enhancement_rounds == 0
    assert artifacts.plan_enhancement_rounds_completed == 0
    assert artifacts.plan_enhancement_converged is False


def test_set_artifacts_atomic_round_trips_plan_enhancement_fields(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Enhancement round trip",
        validation_criteria="Test task completion is observable.",
    )
    manager = TaskArtifactManager(temp_db)

    manager.set_artifacts_atomic(task.id, plan_enhancement_rounds=2)
    manager.set_artifacts_atomic(
        task.id,
        plan_enhancement_rounds_completed=1,
        plan_enhancement_converged=True,
    )
    artifacts = manager.get_artifacts(task.id)

    # Earlier-set counter survives the second partial write.
    assert artifacts.plan_enhancement_rounds == 2
    assert artifacts.plan_enhancement_rounds_completed == 1
    assert artifacts.plan_enhancement_converged is True


def test_set_artifacts_atomic_rejects_negative_plan_enhancement_rounds(
    temp_db,
    sample_project,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Enhancement negative",
        validation_criteria="Test task completion is observable.",
    )
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(TaskArtifactConstraintError) as exc_info:
        manager.set_artifacts_atomic(task.id, plan_enhancement_rounds=-1)

    assert exc_info.value.predicate == "plan_enhancement_rounds_negative"
