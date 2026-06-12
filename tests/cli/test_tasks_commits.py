"""Tests for task commit linking CLI commands."""

from unittest.mock import ANY, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.tasks.main import tasks

pytestmark = pytest.mark.unit


@pytest.fixture
def runner():
    """Create a CLI runner."""
    return CliRunner()


@pytest.fixture
def mock_task_manager():
    """Create a mock task manager."""
    manager = MagicMock()
    return manager


# =============================================================================
# gobby tasks commit link
# =============================================================================


class TestCommitLink:
    """Tests for 'gobby tasks commit link' command."""

    def test_link_commit_success(self, runner, mock_task_manager) -> None:
        """Test linking a commit to a task."""
        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = ["abc123"]
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.link_commit.return_value = mock_task

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            result = runner.invoke(tasks, ["commit", "link", "gt-abc123", "abc123"])

            assert result.exit_code == 0
            mock_task_manager.link_commit.assert_called_with("gt-abc123", "abc123", cwd=ANY)
            assert "abc123" in result.output

    def test_link_commit_echoes_normalized_stored_sha(self, runner, mock_task_manager) -> None:
        """Test linking echoes the SHA stored by the task manager."""
        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = []
        updated_task = MagicMock()
        updated_task.commits = ["abcdef1234567890"]
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.link_commit.return_value = updated_task

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            result = runner.invoke(tasks, ["commit", "link", "gt-abc123", "abc"])

        assert result.exit_code == 0
        assert "Linked commit abcdef1234567890 to task gt-abc123" in result.output
        assert "Linked commit abc to task gt-abc123" not in result.output

    def test_link_commit_uses_project_cwd_from_subdirectory(
        self, runner, mock_task_manager, tmp_path, monkeypatch
    ) -> None:
        """Test link resolves commit SHAs against the project root."""
        project_root = tmp_path / "project"
        subdir = project_root / "nested"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)

        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = ["abc123"]
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.link_commit.return_value = mock_task

        with (
            patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager),
            patch(
                "gobby.cli.tasks.commits.get_project_context",
                return_value={"project_path": project_root},
            ) as mock_context,
        ):
            result = runner.invoke(tasks, ["commit", "link", "gt-abc123", "abc123"])

            assert result.exit_code == 0
            mock_context.assert_called_with(cwd=subdir)
            mock_task_manager.link_commit.assert_called_with(
                "gt-abc123", "abc123", cwd=str(project_root)
            )

    def test_link_commit_task_not_found(self, runner, mock_task_manager) -> None:
        """Test error when task not found."""
        mock_task_manager.link_commit.side_effect = ValueError("Task not found")

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            result = runner.invoke(tasks, ["commit", "link", "gt-nonexistent", "abc123"])

            assert result.exit_code != 0
            assert "not found" in result.output.lower() or "error" in result.output.lower()

    def test_link_commit_requires_task_id(self, runner) -> None:
        """Test that task_id argument is required."""
        result = runner.invoke(tasks, ["commit", "link"])

        assert result.exit_code != 0

    def test_link_commit_requires_commit_sha(self, runner) -> None:
        """Test that commit_sha argument is required."""
        result = runner.invoke(tasks, ["commit", "link", "gt-abc123"])

        assert result.exit_code != 0


# =============================================================================
# gobby tasks commit unlink
# =============================================================================


class TestCommitUnlink:
    """Tests for 'gobby tasks commit unlink' command."""

    def test_unlink_commit_success(self, runner, mock_task_manager) -> None:
        """Test unlinking a commit from a task."""
        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = ["abc123"]
        updated_task = MagicMock()
        updated_task.commits = []
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.unlink_commit.return_value = updated_task

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            result = runner.invoke(tasks, ["commit", "unlink", "gt-abc123", "abc123"])

            assert result.exit_code == 0
            mock_task_manager.unlink_commit.assert_called_with("gt-abc123", "abc123", cwd=ANY)
            assert "Unlinked commit abc123 from task gt-abc123" in result.output

    def test_unlink_commit_not_found_from_subdirectory_reports_noop(
        self, runner, mock_task_manager, tmp_path, monkeypatch
    ) -> None:
        """Test unchanged unlink reports no-op and resolves against the project root."""
        project_root = tmp_path / "project"
        subdir = project_root / "nested"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)

        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = ["def456"]
        updated_task = MagicMock()
        updated_task.commits = ["def456"]
        mock_task_manager.get_task.return_value = mock_task
        mock_task_manager.unlink_commit.return_value = updated_task

        with (
            patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager),
            patch(
                "gobby.cli.tasks.commits.get_project_context",
                return_value={"project_path": project_root},
            ) as mock_context,
        ):
            result = runner.invoke(tasks, ["commit", "unlink", "gt-abc123", "abc123"])

            assert result.exit_code == 0
            mock_context.assert_called_with(cwd=subdir)
            mock_task_manager.unlink_commit.assert_called_with(
                "gt-abc123", "abc123", cwd=str(project_root)
            )
            assert "Commit abc123 not found on task gt-abc123; nothing to unlink" in result.output
            assert "Unlinked commit" not in result.output

    def test_unlink_commit_task_not_found(self, runner, mock_task_manager) -> None:
        """Test error when task not found."""
        mock_task_manager.unlink_commit.side_effect = ValueError("Task not found")

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            result = runner.invoke(tasks, ["commit", "unlink", "gt-nonexistent", "abc123"])

            assert result.exit_code != 0


# =============================================================================
# gobby tasks commit list
# =============================================================================


class TestCommitList:
    """Tests for 'gobby tasks commit list' command."""

    def test_list_commits_success(self, runner, mock_task_manager) -> None:
        """Test listing commits linked to a task."""
        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = ["abc123", "def456", "ghi789"]
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            result = runner.invoke(tasks, ["commit", "list", "gt-abc123"])

            assert result.exit_code == 0
            assert "abc123" in result.output
            assert "def456" in result.output
            assert "ghi789" in result.output

    def test_list_commits_empty(self, runner, mock_task_manager) -> None:
        """Test listing commits when task has none."""
        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            result = runner.invoke(tasks, ["commit", "list", "gt-abc123"])

            assert result.exit_code == 0
            assert "no commit" in result.output.lower() or "0" in result.output

    def test_list_commits_task_not_found(self, runner, mock_task_manager) -> None:
        """Test error when task not found."""
        mock_task_manager.get_task.return_value = None

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            result = runner.invoke(tasks, ["commit", "list", "gt-nonexistent"])

            assert result.exit_code != 0


# =============================================================================
# gobby tasks commit auto
# =============================================================================


class TestCommitAuto:
    """Tests for 'gobby tasks commit auto' command."""

    def test_auto_link_commits_success(self, runner, mock_task_manager) -> None:
        """Test auto-linking commits."""
        from gobby.tasks.commits import AutoLinkResult

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            with patch("gobby.cli.tasks.commits.auto_link_commits") as mock_auto_link:
                mock_auto_link.return_value = AutoLinkResult(
                    linked_tasks={"gt-abc123": ["abc123", "def456"]},
                    total_linked=2,
                    skipped=1,
                )

                result = runner.invoke(tasks, ["commit", "auto"])

                assert result.exit_code == 0
                assert "2" in result.output  # Total linked
                mock_auto_link.assert_called_once()

    def test_auto_link_commits_with_since(self, runner, mock_task_manager) -> None:
        """Test auto-linking commits with --since option."""
        from gobby.tasks.commits import AutoLinkResult

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            with patch("gobby.cli.tasks.commits.auto_link_commits") as mock_auto_link:
                mock_auto_link.return_value = AutoLinkResult(
                    linked_tasks={},
                    total_linked=0,
                    skipped=0,
                )

                result = runner.invoke(tasks, ["commit", "auto", "--since", "1 week ago"])

                assert result.exit_code == 0
                call_kwargs = mock_auto_link.call_args.kwargs
                assert call_kwargs.get("since") == "1 week ago"

    def test_auto_link_commits_with_task_id(self, runner, mock_task_manager) -> None:
        """Test auto-linking commits for specific task."""
        from gobby.tasks.commits import AutoLinkResult

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            with patch("gobby.cli.tasks.commits.auto_link_commits") as mock_auto_link:
                mock_auto_link.return_value = AutoLinkResult(
                    linked_tasks={"gt-abc123": ["abc123"]},
                    total_linked=1,
                    skipped=0,
                )

                result = runner.invoke(tasks, ["commit", "auto", "--task", "gt-abc123"])

                assert result.exit_code == 0
                call_kwargs = mock_auto_link.call_args.kwargs
                assert call_kwargs.get("task_id") == "gt-abc123"

    def test_auto_link_commits_no_matches(self, runner, mock_task_manager) -> None:
        """Test auto-linking when no commits match."""
        from gobby.tasks.commits import AutoLinkResult

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            with patch("gobby.cli.tasks.commits.auto_link_commits") as mock_auto_link:
                mock_auto_link.return_value = AutoLinkResult(
                    linked_tasks={},
                    total_linked=0,
                    skipped=0,
                )

                result = runner.invoke(tasks, ["commit", "auto"])

                assert result.exit_code == 0
                assert "0" in result.output or "no" in result.output.lower()


# =============================================================================
# gobby tasks diff
# =============================================================================


class TestTaskDiff:
    """Tests for 'gobby tasks diff' command."""

    def test_diff_success(self, runner, mock_task_manager) -> None:
        """Test getting diff for a task."""
        from gobby.tasks.commits import TaskDiffResult

        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = ["abc123"]
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            with patch("gobby.cli.tasks.commits.get_task_diff") as mock_diff:
                mock_diff.return_value = TaskDiffResult(
                    diff="diff --git a/file.py b/file.py\n+new line",
                    commits=["abc123"],
                    has_uncommitted_changes=False,
                    file_count=1,
                )

                result = runner.invoke(tasks, ["diff", "gt-abc123"])

                assert result.exit_code == 0
                assert "diff --git" in result.output or "+new line" in result.output

    def test_diff_with_uncommitted(self, runner, mock_task_manager) -> None:
        """Test getting diff with uncommitted changes."""
        from gobby.tasks.commits import TaskDiffResult

        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = ["abc123"]
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            with patch("gobby.cli.tasks.commits.get_task_diff") as mock_diff:
                mock_diff.return_value = TaskDiffResult(
                    diff="diff content",
                    commits=["abc123"],
                    has_uncommitted_changes=True,
                    file_count=2,
                )

                result = runner.invoke(tasks, ["diff", "gt-abc123", "--uncommitted"])

                assert result.exit_code == 0
                call_kwargs = mock_diff.call_args.kwargs
                assert call_kwargs.get("include_uncommitted") is True

    def test_diff_task_not_found(self, runner, mock_task_manager) -> None:
        """Test error when task not found."""
        mock_task_manager.get_task.return_value = None

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            result = runner.invoke(tasks, ["diff", "gt-nonexistent"])

            assert result.exit_code != 0

    def test_diff_no_commits(self, runner, mock_task_manager) -> None:
        """Test diff when task has no commits."""
        from gobby.tasks.commits import TaskDiffResult

        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = []
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            with patch("gobby.cli.tasks.commits.get_task_diff") as mock_diff:
                mock_diff.return_value = TaskDiffResult(
                    diff="",
                    commits=[],
                    has_uncommitted_changes=False,
                    file_count=0,
                )

                result = runner.invoke(tasks, ["diff", "gt-abc123"])

                assert result.exit_code == 0
                assert (
                    "no" in result.output.lower()
                    or "empty" in result.output.lower()
                    or result.output.strip() == ""
                )

    def test_diff_stats_only(self, runner, mock_task_manager) -> None:
        """Test showing diff stats only."""
        from gobby.tasks.commits import TaskDiffResult

        mock_task = MagicMock()
        mock_task.id = "gt-abc123"
        mock_task.commits = ["abc123"]
        mock_task_manager.get_task.return_value = mock_task

        with patch("gobby.cli.tasks.commits.get_task_manager", return_value=mock_task_manager):
            with patch("gobby.cli.tasks.commits.get_task_diff") as mock_diff:
                mock_diff.return_value = TaskDiffResult(
                    diff="diff content",
                    commits=["abc123", "def456"],
                    has_uncommitted_changes=False,
                    file_count=5,
                )

                result = runner.invoke(tasks, ["diff", "gt-abc123", "--stats"])

                assert result.exit_code == 0
                # Should show stats like commits count, file count
                assert "2" in result.output or "5" in result.output
