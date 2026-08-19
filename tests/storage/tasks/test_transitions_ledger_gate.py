from __future__ import annotations

from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager, Task

pytestmark = pytest.mark.unit


def test_close_task_does_not_verify_ledger(temp_db, tmp_path: Path) -> None:
    task = _create_task(temp_db, tmp_path)
    task_manager = LocalTaskManager(temp_db)

    closed = task_manager.close_task(task.id)

    assert closed.closed_at is not None


def _create_task(temp_db: HubDatabase, tmp_path: Path) -> Task:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = LocalProjectManager(temp_db).create(name="ledger-gate", repo_path=str(repo))
    return LocalTaskManager(temp_db).create_task(
        project.id,
        title="Root plan task",
        task_type="epic",
        validation_criteria="Test task completion is observable.",
    )
