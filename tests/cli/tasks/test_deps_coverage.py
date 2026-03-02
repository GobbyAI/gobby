"""Tests for cli/tasks/deps.py — targeting uncovered lines."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.tasks.deps import dep_cmd
from gobby.storage.tasks import Task

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_task(
    task_id: str = "abcd1234-0000-0000-0000-000000000000", title: str = "Test Task"
) -> MagicMock:
    task = MagicMock(spec=Task)
    task.id = task_id
    task.title = title
    return task


# ---------------------------------------------------------------------------
# dep add
# ---------------------------------------------------------------------------
class TestDepAdd:
    @patch("gobby.storage.task_dependencies.TaskDependencyManager")
    @patch("gobby.cli.tasks.deps.resolve_task_id")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_add_success(
        self,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_dep_mgr: MagicMock,
        runner: CliRunner,
    ) -> None:
        task = _mock_task("aaaa1111-0000-0000-0000-000000000000")
        blocker = _mock_task("bbbb2222-0000-0000-0000-000000000000")
        mock_resolve.side_effect = [task, blocker]
        result = runner.invoke(dep_cmd, ["add", "#1", "#2"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Added dependency" in result.output

    @patch("gobby.cli.tasks.deps.resolve_task_id")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_add_task_not_found(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_resolve.return_value = None
        result = runner.invoke(dep_cmd, ["add", "#1", "#2"], catch_exceptions=False)
        assert result.exit_code == 0  # returns early, no error exit

    @patch("gobby.cli.tasks.deps.resolve_task_id")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_add_blocker_not_found(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        task = _mock_task()
        mock_resolve.side_effect = [task, None]
        result = runner.invoke(dep_cmd, ["add", "#1", "#2"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.storage.task_dependencies.TaskDependencyManager")
    @patch("gobby.cli.tasks.deps.resolve_task_id")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_add_value_error(
        self,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_dep_mgr: MagicMock,
        runner: CliRunner,
    ) -> None:
        task = _mock_task("aaaa1111-0000-0000-0000-000000000000")
        blocker = _mock_task("bbbb2222-0000-0000-0000-000000000000")
        mock_resolve.side_effect = [task, blocker]
        mock_dep_mgr.return_value.add_dependency.side_effect = ValueError("cycle detected")
        result = runner.invoke(dep_cmd, ["add", "#1", "#2"], catch_exceptions=False)
        assert result.exit_code == 0  # error printed but no sys.exit
        # The error message goes to stderr via click.echo(err=True)

    @patch("gobby.storage.task_dependencies.TaskDependencyManager")
    @patch("gobby.cli.tasks.deps.resolve_task_id")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_add_with_type(
        self,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_dep_mgr: MagicMock,
        runner: CliRunner,
    ) -> None:
        task = _mock_task("aaaa1111-0000-0000-0000-000000000000")
        blocker = _mock_task("bbbb2222-0000-0000-0000-000000000000")
        mock_resolve.side_effect = [task, blocker]
        result = runner.invoke(
            dep_cmd, ["add", "#1", "#2", "--type", "related"], catch_exceptions=False
        )
        assert result.exit_code == 0
        mock_dep_mgr.return_value.add_dependency.assert_called_once_with(
            task.id, blocker.id, "related"
        )


# ---------------------------------------------------------------------------
# dep remove
# ---------------------------------------------------------------------------
class TestDepRemove:
    @patch("gobby.storage.task_dependencies.TaskDependencyManager")
    @patch("gobby.cli.tasks.deps.resolve_task_id")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_remove_success(
        self,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_dep_mgr: MagicMock,
        runner: CliRunner,
    ) -> None:
        task = _mock_task("aaaa1111-0000-0000-0000-000000000000")
        blocker = _mock_task("bbbb2222-0000-0000-0000-000000000000")
        mock_resolve.side_effect = [task, blocker]
        result = runner.invoke(dep_cmd, ["remove", "#1", "#2"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Removed dependency" in result.output

    @patch("gobby.cli.tasks.deps.resolve_task_id")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_remove_task_not_found(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_resolve.return_value = None
        result = runner.invoke(dep_cmd, ["remove", "#1", "#2"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.tasks.deps.resolve_task_id")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_remove_blocker_not_found(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        task = _mock_task()
        mock_resolve.side_effect = [task, None]
        result = runner.invoke(dep_cmd, ["remove", "#1", "#2"], catch_exceptions=False)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# dep tree
# ---------------------------------------------------------------------------
class TestDepTree:
    @patch("gobby.storage.task_dependencies.TaskDependencyManager")
    @patch("gobby.cli.tasks.deps.resolve_task_id")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_tree_with_blockers_and_blocking(
        self,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_dep_mgr: MagicMock,
        runner: CliRunner,
    ) -> None:
        task = _mock_task("aaaa1111-0000-0000-0000-000000000000", "Main Task")
        mock_resolve.return_value = task
        mock_dep_mgr.return_value.get_dependency_tree.return_value = {
            "blockers": [
                {
                    "id": "bbbb2222-0000-0000-0000-000000000000",
                    "title": "Blocker 1",
                    "status": "closed",
                },
                {
                    "id": "cccc3333-0000-0000-0000-000000000000",
                    "title": "Blocker 2",
                    "status": "open",
                },
            ],
            "blocking": [
                {
                    "id": "dddd4444-0000-0000-0000-000000000000",
                    "title": "Dependent 1",
                    "status": "open",
                },
            ],
        }
        result = runner.invoke(dep_cmd, ["tree", "#1"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Blocked by:" in result.output
        assert "Blocker 1" in result.output
        assert "Blocking:" in result.output
        assert "Dependent 1" in result.output

    @patch("gobby.storage.task_dependencies.TaskDependencyManager")
    @patch("gobby.cli.tasks.deps.resolve_task_id")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_tree_empty(
        self,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_dep_mgr: MagicMock,
        runner: CliRunner,
    ) -> None:
        task = _mock_task("aaaa1111-0000-0000-0000-000000000000", "Solo Task")
        mock_resolve.return_value = task
        mock_dep_mgr.return_value.get_dependency_tree.return_value = {
            "blockers": [],
            "blocking": [],
        }
        result = runner.invoke(dep_cmd, ["tree", "#1"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "(none)" in result.output

    @patch("gobby.cli.tasks.deps.resolve_task_id", return_value=None)
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_tree_task_not_found(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(dep_cmd, ["tree", "#999"], catch_exceptions=False)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# dep cycles
# ---------------------------------------------------------------------------
class TestDepCycles:
    @patch("gobby.storage.task_dependencies.TaskDependencyManager")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_no_cycles(
        self, mock_mgr: MagicMock, mock_dep_mgr: MagicMock, runner: CliRunner
    ) -> None:
        mock_dep_mgr.return_value.check_cycles.return_value = []
        result = runner.invoke(dep_cmd, ["cycles"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No dependency cycles" in result.output

    @patch("gobby.storage.task_dependencies.TaskDependencyManager")
    @patch("gobby.cli.tasks.deps.get_task_manager")
    def test_cycles_found(
        self, mock_mgr: MagicMock, mock_dep_mgr: MagicMock, runner: CliRunner
    ) -> None:
        mock_dep_mgr.return_value.check_cycles.return_value = [
            ["aaaa1111-xxxx", "bbbb2222-yyyy", "aaaa1111-xxxx"],
        ]
        result = runner.invoke(dep_cmd, ["cycles"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "1 dependency cycle" in result.output
