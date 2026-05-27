"""Tests for app-level task_artifacts base enforcement."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import (
    LocalTaskManager,
    MissingIsolationBaseError,
    Task,
    TaskArtifactManager,
)

pytestmark = pytest.mark.unit


def test_new_isolation_write_without_base_raises(temp_db, sample_project) -> None:
    task = _task(temp_db, sample_project)
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(MissingIsolationBaseError):
        manager.set_artifacts_atomic(
            task.id,
            worktree_path="/tmp/wt",
            worktree_id="wt-1",
        )


def test_legacy_row_update_other_field_permitted(temp_db, sample_project) -> None:
    task = _task(temp_db, sample_project)
    _insert_legacy_worktree_row(temp_db, task.id)
    manager = TaskArtifactManager(temp_db)

    artifacts = manager.set_artifacts_atomic(task.id, target_branch="release/0.4")

    assert artifacts.worktree_path == "/tmp/wt"
    assert artifacts.base_commit_sha is None
    assert artifacts.target_branch == "release/0.4"


def test_legacy_row_isolation_modify_requires_base(temp_db, sample_project) -> None:
    task = _task(temp_db, sample_project)
    _insert_legacy_worktree_row(temp_db, task.id)
    manager = TaskArtifactManager(temp_db)

    with pytest.raises(MissingIsolationBaseError):
        manager.set_artifacts_atomic(task.id, worktree_path="/tmp/wt2", worktree_id="wt-2")

    artifacts = manager.set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/wt2",
        worktree_id="wt-2",
        base_commit_sha="abc123",
    )
    assert artifacts.base_commit_sha == "abc123"
    assert artifacts.worktree_path == "/tmp/wt2"


def test_clear_isolation_pair_clears_base(temp_db, sample_project) -> None:
    task = _task(temp_db, sample_project)
    manager = TaskArtifactManager(temp_db)
    manager.set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/wt",
        worktree_id="wt-1",
        base_commit_sha="abc123",
    )

    artifacts = manager.clear_isolation_pair(task.id, "worktree")

    assert artifacts.worktree_path is None
    assert artifacts.worktree_id is None
    assert artifacts.base_commit_sha is None


def _task(temp_db: HubDatabase, sample_project: dict[str, Any]) -> Task:
    return LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Artifacts",
    )


def _insert_legacy_worktree_row(temp_db: HubDatabase, task_id: str) -> None:
    temp_db.execute(
        """
        INSERT INTO task_artifacts (task_id, worktree_path, worktree_id)
        VALUES (%s, '/tmp/wt', 'wt-1')
        """,
        (task_id,),
    )
