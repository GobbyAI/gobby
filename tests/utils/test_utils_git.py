"""Comprehensive tests for git utility functions.

Tests cover:
- run_git_command: success, failure, timeout, file not found, generic exceptions
- get_github_url: origin remote, fallback remotes, no remotes
- get_git_branch: normal branch, detached HEAD, unable to determine branch
- get_git_metadata: normal repo, non-repo, nonexistent path, default cwd, exceptions
"""

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.utils.git import (
    GitMetadata,
    get_git_branch,
    get_git_metadata,
    get_github_url,
    git_subprocess_env,
    is_valid_sha_format,
    normalize_commit_sha,
    run_git_command,
)

pytestmark = pytest.mark.unit


class TestRunGitCommand:
    """Tests for run_git_command function."""

    def test_success_returns_stdout(self, temp_dir: Path) -> None:
        """Test successful git command returns stripped stdout."""
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "  output with whitespace  \n"
            mock_run.return_value = mock_result

            result = run_git_command(["git", "status"], temp_dir)

            assert result == "output with whitespace"
            mock_run.assert_called_once_with(
                ["git", "status"],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

    def test_failure_returns_none(self, temp_dir: Path) -> None:
        """Test failed git command returns None."""
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 128
            mock_result.stderr = "fatal: not a git repository"
            mock_run.return_value = mock_result

            result = run_git_command(["git", "status"], temp_dir)

            assert result is None

    def test_custom_timeout(self, temp_dir: Path) -> None:
        """Test custom timeout is passed to subprocess."""
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "output"
            mock_run.return_value = mock_result

            run_git_command(["git", "status"], temp_dir, timeout=10)

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["timeout"] == 10

    def test_timeout_expired_returns_none(self, temp_dir: Path) -> None:
        """Test TimeoutExpired exception returns None."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=5)

            result = run_git_command(["git", "status"], temp_dir, timeout=5)

            assert result is None

    def test_file_not_found_returns_none(self, temp_dir: Path) -> None:
        """Test FileNotFoundError returns None when git not in PATH."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()

            result = run_git_command(["git", "status"], temp_dir)

            assert result is None

    def test_missing_git_resolution_adds_fallback_path(
        self,
        temp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A sparse daemon PATH still gets standard git install locations."""
        monkeypatch.setenv("PATH", "")
        with (
            patch("gobby.utils.git.shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "ok\n"
            mock_run.return_value = mock_result

            result = run_git_command(["git", "status"], temp_dir)

        assert result == "ok"
        env = mock_run.call_args.kwargs["env"]
        assert "/usr/bin" in env["PATH"]
        assert git_subprocess_env() is not None

    def test_generic_exception_returns_none(self, temp_dir: Path) -> None:
        """Test generic Exception returns None and is logged."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = OSError("Permission denied")

            result = run_git_command(["git", "status"], temp_dir)

            assert result is None

    def test_path_as_string(self, temp_dir: Path) -> None:
        """Test cwd can be passed as string."""
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "output"
            mock_run.return_value = mock_result

            result = run_git_command(["git", "status"], str(temp_dir))

            assert result == "output"

    @pytest.mark.integration
    def test_real_git_command(self, temp_dir: Path) -> None:
        """Integration test with real git command."""
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)

        result = run_git_command(["git", "rev-parse", "--git-dir"], temp_dir)

        assert result is not None
        assert ".git" in result


class TestGetGithubUrl:
    """Tests for get_github_url function."""

    def test_origin_remote_exists(self, temp_dir: Path) -> None:
        """Test returns origin remote URL when it exists."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = "https://github.com/user/repo.git"

            result = get_github_url(temp_dir)

            assert result == "https://github.com/user/repo.git"
            mock_run.assert_called_once_with(["git", "remote", "get-url", "origin"], temp_dir)

    def test_fallback_to_first_remote(self, temp_dir: Path) -> None:
        """Test falls back to first remote when origin doesn't exist."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            # First call: origin doesn't exist
            # Second call: list remotes
            # Third call: get URL for first remote
            mock_run.side_effect = [
                None,  # origin not found
                "upstream\nother",  # list remotes
                "https://github.com/upstream/repo.git",  # upstream URL
            ]

            result = get_github_url(temp_dir)

            assert result == "https://github.com/upstream/repo.git"
            assert mock_run.call_count == 3

    def test_fallback_remote_url_fails(self, temp_dir: Path) -> None:
        """Test returns None when fallback remote URL retrieval fails."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.side_effect = [
                None,  # origin not found
                "upstream",  # list remotes
                None,  # upstream URL fails
            ]

            result = get_github_url(temp_dir)

            assert result is None

    def test_no_remotes(self, temp_dir: Path) -> None:
        """Test returns None when no remotes exist."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.side_effect = [
                None,  # origin not found
                None,  # no remotes
            ]

            result = get_github_url(temp_dir)

            assert result is None

    def test_empty_remote_list(self, temp_dir: Path) -> None:
        """Test returns None when remote list is empty string."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.side_effect = [
                None,  # origin not found
                "",  # empty remote list
            ]

            result = get_github_url(temp_dir)

            # Empty string is truthy split result [""], but [""][0] is ""
            # which is falsy, so URL lookup won't happen
            assert result is None

    @pytest.mark.integration
    def test_real_origin_remote(self, temp_dir: Path) -> None:
        """Integration test with real git repository."""
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        result = get_github_url(temp_dir)

        assert result == "https://github.com/test/repo.git"

    @pytest.mark.integration
    def test_real_no_remote(self, temp_dir: Path) -> None:
        """Integration test with git repo without remotes."""
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)

        result = get_github_url(temp_dir)

        assert result is None


class TestGetGitBranch:
    """Tests for get_git_branch function."""

    def test_returns_branch_name(self, temp_dir: Path) -> None:
        """Test returns branch name from --show-current."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = "feature/my-branch"

            result = get_git_branch(temp_dir)

            assert result == "feature/my-branch"
            mock_run.assert_called_once_with(["git", "branch", "--show-current"], temp_dir)

    def test_detached_head_state(self, temp_dir: Path) -> None:
        """Test returns None in detached HEAD state."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            # First call: --show-current returns empty (detached)
            # Second call: symbolic-ref returns None (confirming detached)
            mock_run.side_effect = [
                None,  # --show-current fails
                None,  # symbolic-ref fails (detached HEAD)
            ]

            result = get_git_branch(temp_dir)

            assert result is None
            assert mock_run.call_count == 2

    def test_unable_to_determine_branch(self, temp_dir: Path) -> None:
        """Test returns None when branch cannot be determined but not detached."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            # First call: --show-current returns empty
            # Second call: symbolic-ref succeeds but we still can't determine
            mock_run.side_effect = [
                None,  # --show-current fails
                "refs/heads/something",  # symbolic-ref succeeds
            ]

            result = get_git_branch(temp_dir)

            # This path returns None with "Unable to determine" log
            assert result is None

    def test_not_a_repo(self, temp_dir: Path) -> None:
        """Test returns None when not in a git repo."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = None

            result = get_git_branch(temp_dir)

            assert result is None

    @pytest.mark.integration
    def test_real_branch_name(self, temp_dir: Path) -> None:
        """Integration test getting real branch name."""
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        (temp_dir / "file.txt").write_text("test")
        subprocess.run(["git", "add", "."], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        result = get_git_branch(temp_dir)

        assert result in ["main", "master"]

    @pytest.mark.integration
    def test_real_detached_head(self, temp_dir: Path) -> None:
        """Integration test in detached HEAD state."""
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        (temp_dir / "file.txt").write_text("test")
        subprocess.run(["git", "add", "."], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        # Checkout specific commit to enter detached HEAD
        subprocess.run(
            ["git", "checkout", "HEAD~0"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        result = get_git_branch(temp_dir)

        assert result is None


class TestGetGitMetadata:
    """Tests for get_git_metadata function."""

    def test_full_metadata(self, temp_dir: Path) -> None:
        """Test returns complete metadata for valid repo."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.side_effect = [
                ".git",  # rev-parse --git-dir
                "https://github.com/user/repo.git",  # get origin URL
                "main",  # get branch
            ]

            result = get_git_metadata(temp_dir)

            assert result["github_url"] == "https://github.com/user/repo.git"
            assert result["git_branch"] == "main"

    def test_not_a_git_repo(self, temp_dir: Path) -> None:
        """Test returns empty dict for non-git directory."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = None  # rev-parse fails

            result = get_git_metadata(temp_dir)

            assert result == {}

    def test_nonexistent_path(self) -> None:
        """Test returns empty dict for nonexistent path."""
        result = get_git_metadata(Path("/nonexistent/path/that/does/not/exist"))

        assert result == {}

    def test_default_cwd(self) -> None:
        """Test uses current working directory when cwd is None."""
        with (
            patch("gobby.utils.git.run_git_command") as mock_run,
            patch("pathlib.Path.cwd") as mock_cwd,
            patch("pathlib.Path.exists") as mock_exists,
        ):
            mock_cwd.return_value = Path("/current/dir")
            mock_exists.return_value = True
            mock_run.return_value = None  # Not a git repo

            result = get_git_metadata(None)

            assert result == {}
            mock_cwd.assert_called_once()

    def test_path_as_string(self, temp_dir: Path) -> None:
        """Test cwd can be passed as string."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = None

            result = get_git_metadata(str(temp_dir))

            assert result == {}

    def test_exception_during_metadata_extraction(self, temp_dir: Path) -> None:
        """Test handles exception during metadata extraction gracefully."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            # First call succeeds (is a git repo)
            # Then get_github_url raises exception
            mock_run.side_effect = [
                ".git",  # rev-parse succeeds
            ]

            with patch("gobby.utils.git.get_github_url") as mock_url:
                mock_url.side_effect = RuntimeError("Unexpected error")

                result = get_git_metadata(temp_dir)

                # Should return empty or partial metadata, not crash
                assert isinstance(result, dict)

    def test_partial_metadata(self, temp_dir: Path) -> None:
        """Test returns partial metadata when some fields unavailable."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.side_effect = [
                ".git",  # rev-parse succeeds
                None,  # no origin remote
                None,  # no remotes at all
                "main",  # branch succeeds
            ]

            result = get_git_metadata(temp_dir)

            assert result.get("github_url") is None
            assert result.get("git_branch") == "main"

    @pytest.mark.integration
    def test_real_metadata(self, temp_dir: Path) -> None:
        """Integration test with real git repository."""
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        (temp_dir / "file.txt").write_text("test")
        subprocess.run(["git", "add", "."], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        result = get_git_metadata(temp_dir)

        assert result["github_url"] == "https://github.com/test/repo.git"
        assert result["git_branch"] in ["main", "master"]


class TestGitMetadataTypeDict:
    """Tests for GitMetadata TypedDict structure."""

    def test_empty_metadata(self) -> None:
        """Test empty GitMetadata is valid."""
        metadata: GitMetadata = {}
        assert metadata == {}

    def test_full_metadata(self) -> None:
        """Test GitMetadata with all fields."""
        metadata: GitMetadata = {
            "github_url": "https://github.com/user/repo.git",
            "git_branch": "main",
        }
        assert metadata["github_url"] == "https://github.com/user/repo.git"
        assert metadata["git_branch"] == "main"

    def test_partial_metadata(self) -> None:
        """Test GitMetadata with only some fields."""
        metadata: GitMetadata = {"github_url": "https://github.com/user/repo.git"}
        assert metadata["github_url"] == "https://github.com/user/repo.git"
        assert "git_branch" not in metadata

    def test_none_values(self) -> None:
        """Test GitMetadata with None values."""
        metadata: GitMetadata = {"github_url": None, "git_branch": None}
        assert metadata["github_url"] is None
        assert metadata["git_branch"] is None


class TestEdgeCases:
    """Edge case tests for git utilities."""

    def test_run_git_command_with_special_characters_in_output(self, temp_dir: Path) -> None:
        """Test handling output with special characters."""
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "branch-with-unicode-\u00e9\u00e8\n"
            mock_run.return_value = mock_result

            result = run_git_command(["git", "branch", "--show-current"], temp_dir)

            assert result == "branch-with-unicode-\u00e9\u00e8"

    def test_get_github_url_with_ssh_format(self, temp_dir: Path) -> None:
        """Test SSH URL format is preserved."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = "git@github.com:user/repo.git"

            result = get_github_url(temp_dir)

            assert result == "git@github.com:user/repo.git"

    def test_get_github_url_multiple_remotes(self, temp_dir: Path) -> None:
        """Test with multiple remotes, first one is used."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.side_effect = [
                None,  # origin not found
                "upstream\nfork\nbackup",  # multiple remotes
                "https://github.com/upstream/repo.git",  # first remote URL
            ]

            result = get_github_url(temp_dir)

            assert result == "https://github.com/upstream/repo.git"
            # Verify it asked for "upstream" (first in list)
            calls = mock_run.call_args_list
            assert calls[2][0][0] == ["git", "remote", "get-url", "upstream"]

    def test_run_git_command_empty_output(self, temp_dir: Path) -> None:
        """Test command with empty output."""
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_run.return_value = mock_result

            result = run_git_command(["git", "status"], temp_dir)

            assert result == ""

    def test_run_git_command_whitespace_only_output(self, temp_dir: Path) -> None:
        """Test command with whitespace-only output."""
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "   \n\t\n  "
            mock_run.return_value = mock_result

            result = run_git_command(["git", "status"], temp_dir)

            assert result == ""

    def test_get_git_branch_empty_branch_name(self, temp_dir: Path) -> None:
        """Test when branch name is empty string."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            # Empty string from --show-current
            mock_run.side_effect = [
                "",  # empty branch name (falsy)
                None,  # symbolic-ref fails
            ]

            result = get_git_branch(temp_dir)

            # Empty string is falsy, so it checks detached HEAD
            assert result is None

    def test_get_git_metadata_handles_path_object(self, temp_dir: Path) -> None:
        """Test Path object handling."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = None

            result = get_git_metadata(temp_dir)

            assert result == {}
            # Verify Path was passed correctly
            mock_run.assert_called_once()


class TestLogging:
    """Tests to verify logging behavior."""

    def test_run_git_command_logs_failure(
        self, temp_dir: Path, caplog, enable_log_propagation
    ) -> None:
        """Test debug logging on command failure."""
        with caplog.at_level(logging.DEBUG, logger="gobby.utils.git"):
            with patch("subprocess.run") as mock_run:
                mock_result = MagicMock()
                mock_result.returncode = 1
                mock_result.stderr = "error message"
                mock_run.return_value = mock_result

                run_git_command(["git", "status"], temp_dir)

        assert "Git command failed" in caplog.text

    def test_run_git_command_logs_timeout(
        self, temp_dir: Path, caplog, enable_log_propagation
    ) -> None:
        """Test warning logging on timeout."""
        with caplog.at_level(logging.DEBUG, logger="gobby.utils.git"):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=5)

                run_git_command(["git", "status"], temp_dir, timeout=5)

        assert "timed out" in caplog.text

    def test_run_git_command_logs_not_found(
        self, temp_dir: Path, caplog, enable_log_propagation
    ) -> None:
        """Test warning logging when git not found."""
        with caplog.at_level(logging.DEBUG, logger="gobby.utils.git"):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = FileNotFoundError()

                run_git_command(["git", "status"], temp_dir)

        assert "not found" in caplog.text

    def test_run_git_command_logs_generic_error(
        self, temp_dir: Path, caplog, enable_log_propagation
    ) -> None:
        """Test error logging on generic exception."""
        with caplog.at_level(logging.DEBUG, logger="gobby.utils.git"):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = PermissionError("Access denied")

                run_git_command(["git", "status"], temp_dir)

        assert "error" in caplog.text.lower()

    def test_get_github_url_logs_fallback(
        self, temp_dir: Path, caplog, enable_log_propagation
    ) -> None:
        """Test debug logging when using fallback remote."""
        with caplog.at_level(logging.DEBUG, logger="gobby.utils.git"):
            with patch("gobby.utils.git.run_git_command") as mock_run:
                mock_run.side_effect = [
                    None,  # origin not found
                    "upstream",  # list remotes
                    "https://github.com/upstream/repo.git",  # upstream URL
                ]

                get_github_url(temp_dir)

        assert "upstream" in caplog.text

    def test_get_github_url_logs_no_remotes(
        self, temp_dir: Path, caplog, enable_log_propagation
    ) -> None:
        """Test debug logging when no remotes found."""
        with caplog.at_level(logging.DEBUG, logger="gobby.utils.git"):
            with patch("gobby.utils.git.run_git_command") as mock_run:
                mock_run.side_effect = [None, None]

                get_github_url(temp_dir)

        assert "No git remotes found" in caplog.text

    def test_get_git_branch_logs_detached(
        self, temp_dir: Path, caplog, enable_log_propagation
    ) -> None:
        """Test debug logging in detached HEAD state."""
        with caplog.at_level(logging.DEBUG, logger="gobby.utils.git"):
            with patch("gobby.utils.git.run_git_command") as mock_run:
                mock_run.side_effect = [None, None]

                get_git_branch(temp_dir)

        assert "detached HEAD" in caplog.text

    def test_get_git_metadata_logs_not_repo(
        self, temp_dir: Path, caplog, enable_log_propagation
    ) -> None:
        """Test debug logging when not a git repo."""
        with caplog.at_level(logging.DEBUG, logger="gobby.utils.git"):
            with patch("gobby.utils.git.run_git_command") as mock_run:
                mock_run.return_value = None

                get_git_metadata(temp_dir)

        assert "Not a git repository" in caplog.text

    def test_get_git_metadata_logs_nonexistent_path(self, caplog, enable_log_propagation) -> None:
        """Test warning logging for nonexistent path."""
        with caplog.at_level(logging.DEBUG, logger="gobby.utils.git"):
            get_git_metadata(Path("/nonexistent/path"))

        assert "does not exist" in caplog.text


class TestNormalizeCommitSha:
    """Tests for normalize_commit_sha function."""

    def test_normalizes_short_sha(self, temp_dir: Path) -> None:
        """Test short SHA is normalized via cat-file + rev-parse --short."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.side_effect = ["commit", "abc1234"]

            result = normalize_commit_sha("abc1234", cwd=temp_dir)

            assert result == "abc1234"
            assert mock_run.call_count == 2
            mock_run.assert_any_call(["git", "cat-file", "-t", "abc1234"], cwd=temp_dir)
            mock_run.assert_any_call(["git", "rev-parse", "--short", "abc1234"], cwd=temp_dir)

    def test_normalizes_full_sha_to_short(self, temp_dir: Path) -> None:
        """Test full SHA is shortened via cat-file + rev-parse --short."""
        full_sha = "abc1234567890abcdef1234567890abcdef123456"
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.side_effect = ["commit", "abc1234"]

            result = normalize_commit_sha(full_sha, cwd=temp_dir)

            assert result == "abc1234"
            assert mock_run.call_count == 2
            mock_run.assert_any_call(["git", "cat-file", "-t", full_sha], cwd=temp_dir)
            mock_run.assert_any_call(["git", "rev-parse", "--short", full_sha], cwd=temp_dir)

    def test_returns_none_for_invalid_sha(self, temp_dir: Path) -> None:
        """Test returns None when git can't resolve the SHA."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = None

            result = normalize_commit_sha("invalidsha", cwd=temp_dir)

            assert result is None

    def test_returns_none_for_non_commit_object(self, temp_dir: Path) -> None:
        """Test returns None when SHA resolves to a tree or blob, not a commit."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = "tree"

            result = normalize_commit_sha("abc1234", cwd=temp_dir)

            assert result is None
            mock_run.assert_called_once_with(["git", "cat-file", "-t", "abc1234"], cwd=temp_dir)

    def test_returns_none_for_blob_object(self, temp_dir: Path) -> None:
        """Test returns None when SHA resolves to a blob."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = "blob"

            result = normalize_commit_sha("abc1234", cwd=temp_dir)

            assert result is None

    def test_returns_none_for_tag_object(self, temp_dir: Path) -> None:
        """Test returns None when SHA resolves to a tag object."""
        with patch("gobby.utils.git.run_git_command") as mock_run:
            mock_run.return_value = "tag"

            result = normalize_commit_sha("abc1234", cwd=temp_dir)

            assert result is None

    def test_returns_none_for_too_short_sha(self) -> None:
        """Test returns None when SHA is less than 4 characters."""
        result = normalize_commit_sha("abc")
        assert result is None

    def test_returns_none_for_empty_sha(self) -> None:
        """Test returns None when SHA is empty."""
        result = normalize_commit_sha("")
        assert result is None

    def test_returns_none_for_none_sha(self) -> None:
        """Test returns None when SHA is None."""
        result = normalize_commit_sha(None)
        assert result is None

    def test_uses_cwd_when_none(self) -> None:
        """Test uses Path.cwd() when cwd is None."""
        with (
            patch("gobby.utils.git.run_git_command") as mock_run,
            patch("pathlib.Path.cwd") as mock_cwd,
        ):
            mock_cwd.return_value = Path("/current/dir")
            mock_run.side_effect = ["commit", "abc1234"]

            result = normalize_commit_sha("abc1234")

            assert result == "abc1234"
            mock_cwd.assert_called_once()
            assert mock_run.call_count == 2
            mock_run.assert_any_call(["git", "cat-file", "-t", "abc1234"], cwd=Path("/current/dir"))
            mock_run.assert_any_call(
                ["git", "rev-parse", "--short", "abc1234"], cwd=Path("/current/dir")
            )

    @pytest.mark.integration
    def test_real_normalize_sha(self, temp_dir: Path) -> None:
        """Integration test normalizing a real commit SHA."""
        # Create a git repo with a commit
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        (temp_dir / "file.txt").write_text("test")
        subprocess.run(["git", "add", "."], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        # Get full SHA
        full_sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        full_sha = full_sha_result.stdout.strip()

        # Normalize both full and short SHA
        normalized_from_full = normalize_commit_sha(full_sha, cwd=temp_dir)
        normalized_from_short = normalize_commit_sha(full_sha[:7], cwd=temp_dir)

        # Both should return the same short SHA
        assert normalized_from_full is not None
        assert normalized_from_short is not None
        assert normalized_from_full == normalized_from_short
        # Should be short (typically 7 chars, but could be more in large repos)
        assert len(normalized_from_full) < 40


class TestIsValidShaFormat:
    """Tests for is_valid_sha_format function."""

    def test_valid_short_sha(self) -> None:
        """Test valid 7-character hex SHA."""
        assert is_valid_sha_format("abc1234") is True

    def test_valid_full_sha(self) -> None:
        """Test valid 40-character hex SHA."""
        assert is_valid_sha_format("a" * 40) is True

    def test_valid_mixed_case(self) -> None:
        """Test valid SHA with mixed case hex."""
        assert is_valid_sha_format("AbCdEf1234") is True

    def test_invalid_non_hex_characters(self) -> None:
        """Test invalid SHA with non-hex characters."""
        assert is_valid_sha_format("ghijklmnop") is False

    def test_invalid_too_short(self) -> None:
        """Test invalid SHA that's too short."""
        assert is_valid_sha_format("abc") is False

    def test_invalid_empty(self) -> None:
        """Test invalid empty SHA."""
        assert is_valid_sha_format("") is False

    def test_minimum_length_valid(self) -> None:
        """Test minimum valid length (4 chars)."""
        assert is_valid_sha_format("abcd") is True

    def test_special_characters_invalid(self) -> None:
        """Test SHA with special characters is invalid."""
        assert is_valid_sha_format("abc123!@") is False

    def test_spaces_invalid(self) -> None:
        """Test SHA with spaces is invalid."""
        assert is_valid_sha_format("abc 123") is False
