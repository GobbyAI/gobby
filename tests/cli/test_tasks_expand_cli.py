"""Tests for task expansion CLI commands."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_expand_compile_failure_marks_run_failed(
    runner: CliRunner, temp_db, sample_project
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Parent task",
        task_type="task",
    )
    run_manager = LocalExpansionRunManager(temp_db)

    async def fail_compile(run_id: str):
        run_manager.start(run_id)
        raise RuntimeError("generation failed")

    service = SimpleNamespace(task_manager=task_manager, compile_run=fail_compile)

    with patch("gobby.cli.tasks.expand._build_expansion_service", return_value=service):
        result = runner.invoke(cli, ["tasks", "expand", "compile", task.id])

    assert result.exit_code == 1
    assert "Error: generation failed" in result.output
    assert "Traceback" not in result.output

    run = run_manager.get_latest_for_task(task.id)
    assert run is not None
    assert run.status == "failed"
    assert run.error == "generation failed"
