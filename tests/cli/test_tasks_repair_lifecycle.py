"""CLI tests for scoped lifecycle repair."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.tasks.repair import repair_lifecycle_cmd
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_repair_lifecycle_cli_requires_scope() -> None:
    runner = CliRunner()

    with patch("gobby.cli.tasks.repair.get_task_manager", return_value=MagicMock()):
        result = runner.invoke(repair_lifecycle_cmd, [])

    assert result.exit_code != 0
    assert "--task or --provenance" in result.output


def test_repair_lifecycle_cli_dry_runs_by_default(hub_db) -> None:
    from gobby.storage.projects import LocalProjectManager

    project = LocalProjectManager(hub_db).create(
        name="test-project",
        repo_path="/tmp/test-project",
        github_url="https://github.com/test/test-project",
    )
    manager = LocalTaskManager(hub_db)
    task = manager.create_task(
        project_id=project.id,
        title="CLI repair task",
        task_type="task",
    )
    manager.initialize_task_manifest(task.id, stage_names=["development"])
    runner = CliRunner()

    with patch("gobby.cli.tasks.repair.get_task_manager", return_value=manager):
        result = runner.invoke(repair_lifecycle_cmd, ["--task", task.id])

    assert result.exit_code == 0
    assert "Dry run: 1 candidate(s)" in result.output
    assert "remove_unused_manifest" in result.output
    assert [row.stage_name for row in manager.stage_states.list_for_task(task.id)] == [
        "development"
    ]
