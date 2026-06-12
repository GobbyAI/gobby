"""Tests for task expansion CLI commands."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.tasks.expand import expand_cmd

pytestmark = pytest.mark.unit


def test_compile_fails_when_run_status_is_not_completed() -> None:
    """Compile exits nonzero when the service leaves the run incomplete."""
    task = SimpleNamespace(id="task-1", project_id="project-1")
    run = SimpleNamespace(
        id="run-1",
        status="failed",
        error="compile failed",
        compiled_spec={},
        to_dict=lambda: {"id": "run-1", "status": "failed"},
    )
    task_manager = MagicMock()
    task_manager.db = MagicMock()

    async def compile_run(run_id: str) -> SimpleNamespace:
        assert run_id == "run-1"
        return run

    service = MagicMock()
    service.task_manager = task_manager
    service.compile_run.side_effect = compile_run
    run_manager = MagicMock()
    run_manager.create.return_value = run

    with (
        patch("gobby.cli.tasks.expand._build_expansion_service", return_value=service),
        patch("gobby.cli.tasks.expand.resolve_task_id", return_value=task),
        patch("gobby.cli.tasks.expand.LocalExpansionRunManager", return_value=run_manager),
        patch("gobby.cli.tasks.expand._resolve_cli_session_id", return_value=None),
    ):
        result = CliRunner().invoke(expand_cmd, ["compile", "#1"])

    assert result.exit_code == 1
    assert "Expansion compile failed: compile failed" in result.output
