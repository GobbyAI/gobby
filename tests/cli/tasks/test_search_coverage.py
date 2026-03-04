"""Tests for cli/tasks/search.py — targeting uncovered lines."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.tasks.search import reindex_tasks, search_tasks

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_task(
    task_id: str = "aaaa1111-0000-0000-0000-000000000000",
    title: str = "Test Task",
    status: str = "open",
    priority: int = 2,
    seq_num: int = 1,
) -> MagicMock:
    task = MagicMock()
    task.id = task_id
    task.title = title
    task.status = status
    task.priority = priority
    task.seq_num = seq_num
    task.to_dict.return_value = {
        "id": task_id,
        "title": title,
        "status": status,
        "priority": priority,
        "seq_num": seq_num,
    }
    return task


# ---------------------------------------------------------------------------
# search_tasks
# ---------------------------------------------------------------------------
class TestSearchTasks:
    @patch("gobby.cli.tasks.search.get_task_manager")
    @patch("gobby.cli.tasks.search.resolve_project_ref", return_value="proj-123")
    def test_search_with_results(
        self, _proj: MagicMock, mock_mgr_fn: MagicMock, runner: CliRunner
    ) -> None:
        mgr = mock_mgr_fn.return_value
        task1 = _mock_task(title="Auth bug", seq_num=1)
        task2 = _mock_task(
            task_id="bbbb1111-0000-0000-0000-000000000000", title="Login fix", seq_num=2
        )
        mgr.search_tasks.return_value = [(task1, 0.95), (task2, 0.72)]
        result = runner.invoke(search_tasks, ["authentication"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "2 task(s)" in result.output
        assert "Auth bug" in result.output

    @patch("gobby.cli.tasks.search.get_task_manager")
    @patch("gobby.cli.tasks.search.resolve_project_ref", return_value="proj-123")
    def test_search_no_results(
        self, _proj: MagicMock, mock_mgr_fn: MagicMock, runner: CliRunner
    ) -> None:
        mgr = mock_mgr_fn.return_value
        mgr.search_tasks.return_value = []
        result = runner.invoke(search_tasks, ["nonexistent"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No tasks found" in result.output

    @patch("gobby.cli.tasks.search.get_task_manager")
    @patch("gobby.cli.tasks.search.resolve_project_ref", return_value="proj-123")
    def test_search_json(self, _proj: MagicMock, mock_mgr_fn: MagicMock, runner: CliRunner) -> None:
        mgr = mock_mgr_fn.return_value
        task1 = _mock_task(title="Auth bug")
        mgr.search_tasks.return_value = [(task1, 0.95)]
        result = runner.invoke(search_tasks, ["auth", "--json"], catch_exceptions=False)
        assert result.exit_code == 0
        assert '"count": 1' in result.output

    @patch("gobby.cli.tasks.search.get_task_manager")
    def test_search_empty_query(self, mock_mgr_fn: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(search_tasks, ["   "], catch_exceptions=False)
        assert result.exit_code == 0  # returns early with error message
        mock_mgr_fn.assert_not_called()

    @patch("gobby.cli.tasks.search.get_task_manager")
    @patch("gobby.cli.tasks.search.resolve_project_ref", return_value="proj-123")
    def test_search_with_status_single(
        self, _proj: MagicMock, mock_mgr_fn: MagicMock, runner: CliRunner
    ) -> None:
        mgr = mock_mgr_fn.return_value
        mgr.search_tasks.return_value = []
        result = runner.invoke(search_tasks, ["test", "--status", "open"], catch_exceptions=False)
        assert result.exit_code == 0
        mgr.search_tasks.assert_called_once()
        call_kwargs = mgr.search_tasks.call_args[1]
        assert call_kwargs["status"] == "open"

    @patch("gobby.cli.tasks.search.get_task_manager")
    @patch("gobby.cli.tasks.search.resolve_project_ref", return_value="proj-123")
    def test_search_with_status_multiple(
        self, _proj: MagicMock, mock_mgr_fn: MagicMock, runner: CliRunner
    ) -> None:
        mgr = mock_mgr_fn.return_value
        mgr.search_tasks.return_value = []
        result = runner.invoke(
            search_tasks, ["test", "--status", "open,in_progress"], catch_exceptions=False
        )
        assert result.exit_code == 0
        call_kwargs = mgr.search_tasks.call_args[1]
        assert call_kwargs["status"] == ["open", "in_progress"]

    @patch("gobby.cli.tasks.search.get_task_manager")
    @patch("gobby.cli.tasks.search.resolve_project_ref", return_value="proj-123")
    def test_search_with_type_and_priority(
        self, _proj: MagicMock, mock_mgr_fn: MagicMock, runner: CliRunner
    ) -> None:
        mgr = mock_mgr_fn.return_value
        mgr.search_tasks.return_value = []
        result = runner.invoke(
            search_tasks,
            ["test", "--type", "bug", "--priority", "1", "--limit", "5", "--min-score", "0.5"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        call_kwargs = mgr.search_tasks.call_args[1]
        assert call_kwargs["task_type"] == "bug"
        assert call_kwargs["priority"] == 1
        assert call_kwargs["limit"] == 5
        assert call_kwargs["min_score"] == 0.5

    @patch("gobby.cli.tasks.search.get_task_manager")
    def test_search_all_projects(self, mock_mgr_fn: MagicMock, runner: CliRunner) -> None:
        mgr = mock_mgr_fn.return_value
        mgr.search_tasks.return_value = []
        result = runner.invoke(search_tasks, ["test", "--all-projects"], catch_exceptions=False)
        assert result.exit_code == 0
        call_kwargs = mgr.search_tasks.call_args[1]
        assert call_kwargs["project_id"] is None

    @patch("gobby.cli.tasks.search.get_task_manager")
    @patch("gobby.cli.tasks.search.resolve_project_ref", return_value="proj-123")
    def test_search_with_project_ref(
        self, _proj: MagicMock, mock_mgr_fn: MagicMock, runner: CliRunner
    ) -> None:
        mgr = mock_mgr_fn.return_value
        mgr.search_tasks.return_value = []
        result = runner.invoke(
            search_tasks, ["test", "--project", "my-project"], catch_exceptions=False
        )
        assert result.exit_code == 0

    @patch("gobby.cli.tasks.search.get_task_manager")
    @patch("gobby.cli.tasks.search.resolve_project_ref", return_value="proj-123")
    def test_search_task_without_seq_num(
        self, _proj: MagicMock, mock_mgr_fn: MagicMock, runner: CliRunner
    ) -> None:
        mgr = mock_mgr_fn.return_value
        task = _mock_task(seq_num=0)  # falsy seq_num
        mgr.search_tasks.return_value = [(task, 0.8)]
        result = runner.invoke(search_tasks, ["test"], catch_exceptions=False)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# reindex_tasks
# ---------------------------------------------------------------------------
class TestReindexTasks:
    @patch("gobby.cli.tasks.search.get_task_manager")
    @patch("gobby.cli.tasks.search.resolve_project_ref", return_value="proj-123")
    def test_reindex(self, _proj: MagicMock, mock_mgr_fn: MagicMock, runner: CliRunner) -> None:
        mgr = mock_mgr_fn.return_value
        mgr.reindex_search.return_value = {"item_count": 42, "vocabulary_size": 100}
        result = runner.invoke(reindex_tasks, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "42" in result.output
        assert "Vocabulary size" in result.output or "100" in result.output

    @patch("gobby.cli.tasks.search.get_task_manager")
    def test_reindex_all_projects(self, mock_mgr_fn: MagicMock, runner: CliRunner) -> None:
        mgr = mock_mgr_fn.return_value
        mgr.reindex_search.return_value = {"item_count": 10}
        result = runner.invoke(reindex_tasks, ["--all-projects"], catch_exceptions=False)
        assert result.exit_code == 0
        mgr.reindex_search.assert_called_once_with(None)

    @patch("gobby.cli.tasks.search.get_task_manager")
    @patch("gobby.cli.tasks.search.resolve_project_ref", return_value="proj-123")
    def test_reindex_no_vocabulary(
        self, _proj: MagicMock, mock_mgr_fn: MagicMock, runner: CliRunner
    ) -> None:
        mgr = mock_mgr_fn.return_value
        mgr.reindex_search.return_value = {"item_count": 5}
        result = runner.invoke(reindex_tasks, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "5" in result.output
