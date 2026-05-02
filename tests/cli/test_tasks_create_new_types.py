"""CLI creation surface contracts for Phase 5 task types."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli

pytestmark = pytest.mark.unit


def test_create_simple_fix() -> None:
    task = MagicMock()
    task.id = "task-1"
    task.seq_num = 1
    task.title = "Small fix"

    with (
        patch("gobby.cli.tasks.crud.get_project_context", return_value={"id": "proj-1"}),
        patch("gobby.cli.tasks.crud.get_task_manager") as get_manager,
    ):
        manager = MagicMock()
        manager.create_task.return_value = task
        get_manager.return_value = manager

        result = CliRunner().invoke(
            cli,
            ["tasks", "create", "Small fix", "--type", "simple_fix"],
        )

    assert result.exit_code == 0
    assert manager.create_task.call_args.kwargs["task_type"] == "simple_fix"
