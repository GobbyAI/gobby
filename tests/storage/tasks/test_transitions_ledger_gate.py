from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_close_task_invokes_verify_when_companion_exists(temp_db, tmp_path: Path) -> None:
    task = _create_task(temp_db, tmp_path)
    task_manager = LocalTaskManager(temp_db)

    with (
        patch(
            "gobby.storage.tasks._transitions.bootstrap_ledger_path_for_task",
            return_value=tmp_path / "ledger.yaml",
        ),
        patch("gobby.storage.tasks._transitions.verify_bootstrap_ledger") as verify,
    ):
        task_manager.close_task(task.id)

    verify.assert_called_once_with(temp_db, task.id)


def test_close_task_skips_verify_when_no_companion(temp_db, tmp_path: Path) -> None:
    task = _create_task(temp_db, tmp_path)
    task_manager = LocalTaskManager(temp_db)

    with (
        patch(
            "gobby.storage.tasks._transitions.bootstrap_ledger_path_for_task",
            return_value=None,
        ),
        patch("gobby.storage.tasks._transitions.verify_bootstrap_ledger") as verify,
    ):
        task_manager.close_task(task.id)

    verify.assert_not_called()


def _create_task(temp_db, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = LocalProjectManager(temp_db).create(name="ledger-gate", repo_path=str(repo))
    return LocalTaskManager(temp_db).create_task(
        project.id,
        title="Root plan task",
        task_type="epic",
    )
