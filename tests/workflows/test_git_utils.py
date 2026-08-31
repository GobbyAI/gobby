"""Tests for git utility functions in workflows.

This module tests the git_utils.py functions which provide
pure utility functions for git operations without ActionContext dependency.
"""

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.workflows.git_utils import (
    DEFAULT_GIT_STATUS_TIMEOUT_SECONDS,
    get_dirty_files_categorized,
    get_file_changes,
    get_git_diff_summary,
    get_git_status,
    get_recent_git_commits,
    resolve_git_worktree_root,
)

pytestmark = pytest.mark.unit


class TestWorktreeRootResolution:
    def test_returns_first_git_worktree_root(self, tmp_path) -> None:
        non_repo = tmp_path / "plain"
        non_repo.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        nested = repo / "nested"
        nested.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)

        assert resolve_git_worktree_root(non_repo, nested) == str(repo.resolve())

    def test_non_git_cwd_dirty_files_returns_empty_without_warning(
        self, tmp_path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="gobby.workflows.git_utils")

        dirty = get_dirty_files_categorized(str(tmp_path))

        assert not dirty
        assert dirty.all == set()
        assert [
            record
            for record in caplog.records
            if record.levelno >= logging.WARNING and "get_dirty_files" in record.getMessage()
        ] == []


class TestGetDirtyFilesCategorized:
    def test_parses_porcelain_paths_without_truncation(self, tmp_path) -> None:
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=tmp_path,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)

        for path in ("modified.txt", "deleted.txt", "old name.txt"):
            (tmp_path / path).write_text("original\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True
        )

        (tmp_path / "modified.txt").write_text("modified\n")
        (tmp_path / "deleted.txt").unlink()
        subprocess.run(
            ["git", "mv", "old name.txt", 'renamed "café" file.txt'],
            cwd=tmp_path,
            check=True,
        )
        (tmp_path / "added file.txt").write_text("added\n")
        subprocess.run(["git", "add", "added file.txt"], cwd=tmp_path, check=True)
        (tmp_path / "untracked ünicode.txt").write_text("untracked\n")

        dirty = get_dirty_files_categorized(str(tmp_path))

        assert dirty.tracked == {
            "added file.txt",
            "deleted.txt",
            "modified.txt",
            'renamed "café" file.txt',
        }
        assert dirty.untracked == {"untracked ünicode.txt"}

    def test_default_timeout_is_headroom_not_a_working_budget(self, tmp_path: Path) -> None:
        """A caller parked here holds one of the workflow runtime's few threads."""
        with (
            patch(
                "gobby.workflows.git_utils.resolve_git_worktree_root",
                return_value=str(tmp_path),
            ),
            patch("gobby.workflows.git_utils.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=b"")
            get_dirty_files_categorized(str(tmp_path))

        assert mock_run.call_args.kwargs["timeout"] == DEFAULT_GIT_STATUS_TIMEOUT_SECONDS

    def test_caller_supplied_timeout_bounds_the_scan(self, tmp_path: Path) -> None:
        with (
            patch(
                "gobby.workflows.git_utils.resolve_git_worktree_root",
                return_value=str(tmp_path),
            ),
            patch("gobby.workflows.git_utils.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=b"")
            get_dirty_files_categorized(str(tmp_path), timeout=1.5)

        assert mock_run.call_args.kwargs["timeout"] == 1.5

    def test_timeout_reports_a_clean_tree_and_names_the_budget(
        self,
        tmp_path: Path,
        enable_log_propagation: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The empty result is indistinguishable from a clean tree, so the log must say."""
        caplog.set_level(logging.WARNING, logger="gobby.workflows.git_utils")

        with (
            patch(
                "gobby.workflows.git_utils.resolve_git_worktree_root",
                return_value=str(tmp_path),
            ),
            patch(
                "gobby.workflows.git_utils.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1.5),
            ),
        ):
            dirty = get_dirty_files_categorized(str(tmp_path), timeout=1.5)

        assert dirty.all == set()
        assert "git status timed out after 1.5s" in caplog.text


class TestGetGitStatus:
    """Tests for get_git_status function."""

    def test_returns_short_status(self) -> None:
        """Test that git status --short output is returned."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="M file.py\nA new_file.py")
            result = get_git_status()

            assert result == "M file.py\nA new_file.py"
            mock_run.assert_called_once_with(
                ["git", "status", "--short"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=None,
            )

    def test_uses_explicit_project_path_from_different_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target_repo = tmp_path / "target"
        target_repo.mkdir()
        subprocess.run(["git", "init"], cwd=target_repo, check=True, capture_output=True)
        (target_repo / "target.txt").write_text("target\n")
        other_cwd = tmp_path / "other"
        other_cwd.mkdir()
        monkeypatch.chdir(other_cwd)

        assert get_git_status(str(target_repo)) == "?? target.txt"

    def test_returns_no_changes_when_empty(self) -> None:
        """Test that 'No changes' is returned when status is empty."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            result = get_git_status()

            assert result == "No changes"

    def test_returns_no_changes_when_whitespace_only(self) -> None:
        """Test that 'No changes' is returned when status is whitespace."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="   \n  \t  ")
            result = get_git_status()

            assert result == "No changes"

    def test_handles_subprocess_timeout(self) -> None:
        """Test graceful handling of subprocess timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=5)
            result = get_git_status()

            assert result == "Not a git repository or git not available"

    def test_handles_file_not_found_error(self) -> None:
        """Test graceful handling when git is not installed."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = get_git_status()

            assert result == "Not a git repository or git not available"

    def test_handles_permission_error(self) -> None:
        """Test graceful handling of permission errors."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = PermissionError("Permission denied")
            result = get_git_status()

            assert result == "Not a git repository or git not available"

    def test_handles_generic_exception(self) -> None:
        """Test graceful handling of unexpected exceptions."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("Unexpected error")
            result = get_git_status()

            assert result == "Not a git repository or git not available"

    def test_handles_not_a_git_repo(self) -> None:
        """Test handling when directory is not a git repository."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(returncode=128, cmd="git status")
            result = get_git_status()

            assert result == "Not a git repository or git not available"

    def test_strips_output(self) -> None:
        """Test that output is properly stripped of whitespace."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="  M file.py  \n")
            result = get_git_status()

            assert result == "M file.py"

    def test_handles_multiple_files(self) -> None:
        """Test handling of multiple changed files."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="M src/file1.py\nA src/file2.py\nD src/deleted.py\n?? untracked.txt"
            )
            result = get_git_status()

            assert "M src/file1.py" in result
            assert "A src/file2.py" in result
            assert "D src/deleted.py" in result
            assert "?? untracked.txt" in result


class TestGetRecentGitCommits:
    """Tests for get_recent_git_commits function."""

    def test_returns_commits_with_hash_and_message(self) -> None:
        """Test that commits are parsed correctly with hash and message."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123def456|feat: add feature\n789xyz000111|fix: bug fix",
            )
            result = get_recent_git_commits()

            assert len(result) == 2
            assert result[0] == {"hash": "abc123def456", "message": "feat: add feature"}
            assert result[1] == {"hash": "789xyz000111", "message": "fix: bug fix"}

    def test_default_max_commits_is_10(self) -> None:
        """Test that default max_commits parameter is 10."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            get_recent_git_commits()

            mock_run.assert_called_once_with(
                ["git", "log", "-10", "--format=%H|%s"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=None,
            )
            assert mock_run.call_count == 1
            assert mock_run.call_args is not None

    def test_custom_max_commits(self) -> None:
        """Test that custom max_commits parameter is respected."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            get_recent_git_commits(max_commits=5)

            mock_run.assert_called_once_with(
                ["git", "log", "-5", "--format=%H|%s"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=None,
            )
            assert mock_run.call_count == 1
            assert mock_run.call_args is not None

    def test_uses_explicit_project_path_from_different_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target_repo = tmp_path / "target"
        target_repo.mkdir()
        subprocess.run(["git", "init"], cwd=target_repo, check=True, capture_output=True)
        (target_repo / "tracked.txt").write_text("tracked\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=target_repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "target commit",
            ],
            cwd=target_repo,
            check=True,
            capture_output=True,
        )
        other_cwd = tmp_path / "other"
        other_cwd.mkdir()
        monkeypatch.chdir(other_cwd)

        commits = get_recent_git_commits(project_path=str(target_repo))

        assert commits[0]["message"] == "target commit"

    def test_returns_empty_list_on_non_zero_returncode(self) -> None:
        """Test that empty list is returned when git command fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = get_recent_git_commits()

            assert result == []

    def test_returns_empty_list_on_exception(self) -> None:
        """Test that empty list is returned on exception."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("Git error")
            result = get_recent_git_commits()

            assert result == []

    def test_handles_timeout(self) -> None:
        """Test graceful handling of subprocess timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=5)
            result = get_recent_git_commits()

            assert result == []

    def test_handles_file_not_found(self) -> None:
        """Test graceful handling when git is not installed."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = get_recent_git_commits()

            assert result == []

    def test_skips_lines_without_pipe(self) -> None:
        """Test that lines without pipe separator are skipped."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123|valid commit\ninvalid line without pipe\nxyz789|another valid",
            )
            result = get_recent_git_commits()

            assert len(result) == 2
            assert result[0]["hash"] == "abc123"
            assert result[1]["hash"] == "xyz789"

    def test_handles_empty_output(self) -> None:
        """Test handling of empty git log output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = get_recent_git_commits()

            assert result == []

    def test_handles_whitespace_only_output(self) -> None:
        """Test handling of whitespace-only git log output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="  \n\t  \n")
            result = get_recent_git_commits()

            assert result == []

    def test_handles_message_with_multiple_pipes(self) -> None:
        """Test that messages containing pipes are handled correctly."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123|feat: add pipe | handling in message",
            )
            result = get_recent_git_commits()

            assert len(result) == 1
            assert result[0]["hash"] == "abc123"
            assert result[0]["message"] == "feat: add pipe | handling in message"

    def test_handles_single_commit(self) -> None:
        """Test handling of single commit."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123|initial commit")
            result = get_recent_git_commits()

            assert len(result) == 1
            assert result[0] == {"hash": "abc123", "message": "initial commit"}

    def test_max_commits_zero(self) -> None:
        """Test behavior with max_commits=0."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = get_recent_git_commits(max_commits=0)

            mock_run.assert_called_once_with(
                ["git", "log", "-0", "--format=%H|%s"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=None,
            )
            assert result == []

    def test_max_commits_large_number(self) -> None:
        """Test behavior with large max_commits value."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc|msg")
            get_recent_git_commits(max_commits=1000)

            mock_run.assert_called_once_with(
                ["git", "log", "-1000", "--format=%H|%s"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=None,
            )
            assert mock_run.call_count == 1
            assert mock_run.call_args is not None


class TestGetFileChanges:
    """Tests for get_file_changes function."""

    def test_returns_modified_and_untracked(self) -> None:
        """Test that both modified and untracked files are returned."""
        with patch("subprocess.run") as mock_run:
            # Mock diff result (first call) and untracked result (second call)
            diff_result = MagicMock(stdout="M\tfile1.py\nD\tfile2.py")
            untracked_result = MagicMock(stdout="new_file.txt")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            assert "Modified/Deleted:" in result
            assert "file1.py" in result
            assert "file2.py" in result
            assert "Untracked:" in result
            assert "new_file.txt" in result

    def test_calls_correct_git_commands(self) -> None:
        """Test that correct git commands are called."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            get_file_changes()

            assert mock_run.call_count == 2
            # First call: git diff HEAD --name-status
            mock_run.assert_any_call(
                ["git", "diff", "HEAD", "--name-status"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=None,
            )
            # Second call: git ls-files --others --exclude-standard
            mock_run.assert_any_call(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=None,
            )

    def test_passes_project_path_to_git_commands(self) -> None:
        """Test that file changes can be collected from a specific project path."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")

            get_file_changes(project_path="/workspace/project")

            assert mock_run.call_count == 2
            mock_run.assert_any_call(
                ["git", "diff", "HEAD", "--name-status"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd="/workspace/project",
            )
            mock_run.assert_any_call(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd="/workspace/project",
            )

    def test_returns_no_changes_when_both_empty(self) -> None:
        """Test that 'No changes' is returned when no changes exist."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            result = get_file_changes()

            assert result == "No changes"

    def test_returns_only_modified_when_no_untracked(self) -> None:
        """Test output when there are only modified files."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="M\tfile.py")
            untracked_result = MagicMock(stdout="")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            assert "Modified/Deleted:" in result
            assert "file.py" in result
            assert "Untracked:" not in result

    def test_returns_only_untracked_when_no_modified(self) -> None:
        """Test output when there are only untracked files."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="")
            untracked_result = MagicMock(stdout="new_file.txt")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            assert "Modified/Deleted:" not in result
            assert "Untracked:" in result
            assert "new_file.txt" in result

    def test_handles_exception(self) -> None:
        """Test graceful handling of exceptions."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("Git error")
            result = get_file_changes()

            assert result == "Unable to determine file changes"

    def test_handles_timeout(self) -> None:
        """Test graceful handling of subprocess timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=5)
            result = get_file_changes()

            assert result == "Unable to determine file changes"

    def test_handles_file_not_found(self) -> None:
        """Test graceful handling when git is not installed."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = get_file_changes()

            assert result == "Unable to determine file changes"

    def test_handles_permission_error(self) -> None:
        """Test graceful handling of permission errors."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = PermissionError("Permission denied")
            result = get_file_changes()

            assert result == "Unable to determine file changes"

    def test_handles_exception_on_second_call(self) -> None:
        """Test handling when second git command fails."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="M\tfile.py")
            mock_run.side_effect = [diff_result, Exception("Second command failed")]

            result = get_file_changes()

            assert result == "Unable to determine file changes"

    def test_strips_whitespace_from_output(self) -> None:
        """Test that whitespace is properly stripped."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="  M\tfile.py  \n")
            untracked_result = MagicMock(stdout="  new.txt  \n")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            # The individual outputs should be stripped
            assert "M\tfile.py" in result
            assert "new.txt" in result

    def test_handles_multiple_modified_files(self) -> None:
        """Test handling of multiple modified files."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="M\tfile1.py\nA\tfile2.py\nD\tfile3.py")
            untracked_result = MagicMock(stdout="")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            assert "file1.py" in result
            assert "file2.py" in result
            assert "file3.py" in result

    def test_handles_multiple_untracked_files(self) -> None:
        """Test handling of multiple untracked files."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="")
            untracked_result = MagicMock(stdout="file1.txt\nfile2.txt\nfile3.txt")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            assert "file1.txt" in result
            assert "file2.txt" in result
            assert "file3.txt" in result

    def test_handles_whitespace_only_diff_output(self) -> None:
        """Test handling when diff output is whitespace only."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="   \n  \t  ")
            untracked_result = MagicMock(stdout="new.txt")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            assert "Modified/Deleted:" not in result
            assert "Untracked:" in result
            assert "new.txt" in result

    def test_handles_whitespace_only_untracked_output(self) -> None:
        """Test handling when untracked output is whitespace only."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="M\tfile.py")
            untracked_result = MagicMock(stdout="   \n  \t  ")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            assert "Modified/Deleted:" in result
            assert "file.py" in result
            assert "Untracked:" not in result

    def test_output_format_with_newlines(self) -> None:
        """Test that output format includes proper newlines between sections."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="M\tmodified.py")
            untracked_result = MagicMock(stdout="untracked.txt")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            # Verify the format includes newline before "Untracked:"
            lines = result.split("\n")
            assert "Modified/Deleted:" in lines[0]
            # There should be an empty line before Untracked section
            assert any("Untracked:" in line for line in lines)


class TestGitUtilsIntegration:
    """Integration-style tests for git utilities (still using mocks but testing combinations)."""

    def test_all_functions_handle_not_a_repo(self) -> None:
        """Test that all functions gracefully handle not being in a git repo."""
        error = subprocess.CalledProcessError(returncode=128, cmd="git")

        with patch("subprocess.run", side_effect=error):
            status = get_git_status()
            commits = get_recent_git_commits()
            changes = get_file_changes()

            assert "Not a git repository" in status
            assert commits == []
            assert "Unable to determine" in changes

    def test_all_functions_handle_git_not_installed(self) -> None:
        """Test that all functions gracefully handle git not being installed."""
        with patch("subprocess.run", side_effect=FileNotFoundError("git")):
            status = get_git_status()
            commits = get_recent_git_commits()
            changes = get_file_changes()

            assert "Not a git repository" in status
            assert commits == []
            assert "Unable to determine" in changes

    def test_all_functions_handle_timeout(self) -> None:
        """Test that all functions gracefully handle timeouts."""
        timeout_error = subprocess.TimeoutExpired(cmd="git", timeout=5)

        with patch("subprocess.run", side_effect=timeout_error):
            status = get_git_status()
            commits = get_recent_git_commits()
            changes = get_file_changes()

            assert "Not a git repository" in status
            assert commits == []
            assert "Unable to determine" in changes


class TestEdgeCases:
    """Edge case tests for git utilities."""

    def test_get_git_status_with_unicode_filenames(self) -> None:
        """Test handling of unicode characters in filenames."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="M test_\u00e9\u00e0\u00fc.py")
            result = get_git_status()

            assert "test_\u00e9\u00e0\u00fc.py" in result

    def test_get_recent_commits_with_special_characters_in_message(self) -> None:
        """Test handling of special characters in commit messages."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='abc123|feat: add "quotes" and \\backslash',
            )
            result = get_recent_git_commits()

            assert len(result) == 1
            assert 'feat: add "quotes" and \\backslash' in result[0]["message"]

    def test_get_file_changes_with_spaces_in_filenames(self) -> None:
        """Test handling of filenames with spaces."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="M\tmy file with spaces.py")
            untracked_result = MagicMock(stdout="another file.txt")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            assert "my file with spaces.py" in result
            assert "another file.txt" in result

    def test_get_recent_commits_with_empty_message(self) -> None:
        """Test handling of commits with empty messages."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123|\nxyz789|normal message",
            )
            result = get_recent_git_commits()

            assert len(result) == 2
            assert result[0] == {"hash": "abc123", "message": ""}
            assert result[1] == {"hash": "xyz789", "message": "normal message"}

    def test_get_git_status_with_binary_files(self) -> None:
        """Test handling of binary file indicators in status."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="M  image.png\nM  data.bin")
            result = get_git_status()

            assert "image.png" in result
            assert "data.bin" in result

    def test_get_file_changes_with_renamed_files(self) -> None:
        """Test handling of renamed files in diff output."""
        with patch("subprocess.run") as mock_run:
            diff_result = MagicMock(stdout="R100\told_name.py\tnew_name.py")
            untracked_result = MagicMock(stdout="")
            mock_run.side_effect = [diff_result, untracked_result]

            result = get_file_changes()

            assert "old_name.py" in result
            assert "new_name.py" in result


class TestGetGitDiffSummary:
    """Tests for get_git_diff_summary."""

    def test_returns_stat_and_diff(self) -> None:
        """Test that both stat and diff are returned."""
        with patch("gobby.workflows.git_utils.subprocess.run") as mock_run:
            stat_result = MagicMock(stdout=" file.py | 2 +-\n 1 file changed", returncode=0)
            diff_result = MagicMock(stdout="diff --git a/file.py\n+new line", returncode=0)
            mock_run.side_effect = [stat_result, diff_result]

            result = get_git_diff_summary()

        assert "### Diff Summary" in result
        assert "### Actual Changes" in result
        assert "file.py" in result

    def test_truncates_long_diff(self) -> None:
        """Test that long diffs are truncated."""
        with patch("gobby.workflows.git_utils.subprocess.run") as mock_run:
            stat_result = MagicMock(stdout="file.py | 2 +-", returncode=0)
            diff_result = MagicMock(stdout="x" * 10000, returncode=0)
            mock_run.side_effect = [stat_result, diff_result]

            result = get_git_diff_summary(max_chars=1000)

        assert "truncated" in result
        assert "x" * 1000 in result

    def test_returns_empty_when_no_changes(self) -> None:
        """Test empty string when no changes."""
        with patch("gobby.workflows.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)

            result = get_git_diff_summary()

        assert result == ""

    def test_handles_timeout(self) -> None:
        """Test graceful handling of subprocess timeout."""
        with patch("gobby.workflows.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)

            result = get_git_diff_summary()

        assert result == ""

    def test_falls_back_to_cached(self) -> None:
        """Test fallback to staged changes."""
        with patch("gobby.workflows.git_utils.subprocess.run") as mock_run:
            empty = MagicMock(stdout="", returncode=0)
            cached_stat = MagicMock(stdout="staged.py | 1 +", returncode=0)
            cached_diff = MagicMock(stdout="diff staged content", returncode=0)
            mock_run.side_effect = [empty, empty, cached_diff, cached_stat]

            result = get_git_diff_summary()

        assert "staged.py | 1 +" in result
        assert "diff staged content" in result
        stat_pos = result.index("staged.py | 1 +")
        diff_pos = result.index("diff staged content")
        assert stat_pos < diff_pos

    def test_handles_exception(self) -> None:
        """Test graceful handling of exceptions."""
        with patch("gobby.workflows.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("git not found")

            result = get_git_diff_summary()

        assert result == ""
