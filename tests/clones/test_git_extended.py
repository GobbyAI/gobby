from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from gobby.clones.git import CloneGitManager

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _managed_clones_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Let creation-path safety checks accept temp clone paths in unit tests."""
    monkeypatch.setattr("gobby.clones.git.CLONES_ROOT", tmp_path)


class TestCloneGitManagerFullClone:
    """Tests for CloneGitManager.full_clone method."""

    @pytest.fixture
    def manager(self, tmp_path: Path) -> CloneGitManager:
        """Create manager with temp directory as repo path."""
        from gobby.clones.git import CloneGitManager

        return CloneGitManager(repo_path=tmp_path)

    @pytest.fixture
    def mock_run(self):
        """Mock subprocess.run."""
        with patch("subprocess.run") as mock:
            yield mock

    def test_full_clone_success(self, manager, mock_run, tmp_path: Path) -> None:
        """Full clone creates clone without depth limit."""
        mock_run.return_value = MagicMock(returncode=0, stdout="Cloning into 'clone'...", stderr="")
        clone_path = tmp_path / "test_clone"

        result = manager.full_clone(
            remote_url="https://github.com/user/repo.git",
            clone_path=clone_path,
            branch="main",
        )

        assert result.success is True

        # First call is the clone, subsequent calls may be git config
        clone_call = mock_run.call_args_list[0]
        cmd = clone_call[0][0]
        assert "clone" in cmd
        assert "--depth" not in cmd
        assert "--single-branch" not in cmd

    def test_full_clone_removes_partial_dir_after_git_error(
        self, manager, mock_run, tmp_path: Path
    ) -> None:
        """Full clone cleans up a partial clone after git exits non-zero."""
        clone_path = tmp_path / "test_clone"

        def run_clone_with_partial_dir(*_args: object, **_kwargs: object) -> MagicMock:
            clone_path.mkdir()
            (clone_path / ".git").mkdir()
            return MagicMock(returncode=128, stdout="", stderr="fatal: repository not found")

        mock_run.side_effect = run_clone_with_partial_dir

        result = manager.full_clone(
            remote_url="https://github.com/user/nonexistent.git",
            clone_path=clone_path,
            branch="main",
        )

        assert result.success is False
        assert not clone_path.exists()


class TestCloneGitManagerCreateClone:
    """Tests for CloneGitManager.create_clone method."""

    @pytest.fixture
    def manager(self, tmp_path: Path) -> CloneGitManager:
        """Create manager with temp directory as repo path."""
        from gobby.clones.git import CloneGitManager

        return CloneGitManager(repo_path=tmp_path)

    @pytest.fixture
    def mock_run(self):
        """Mock subprocess.run."""
        with patch("subprocess.run") as mock:
            yield mock

    def test_create_shallow_clone_success(self, manager, mock_run, tmp_path: Path) -> None:
        """Create clone executes shallow clone and returns success."""
        from gobby.clones.git import GitOperationResult

        # Mock get_remote_url
        with patch.object(
            manager, "get_remote_url", return_value="https://github.com/user/repo.git"
        ):
            # Mock shallow_clone
            with patch.object(manager, "shallow_clone") as mock_shallow:
                # Use real object instead of MagicMock
                mock_shallow.return_value = GitOperationResult(
                    success=True, message="Cloned", output="Cloned"
                )

                # Mock _run_git to avoid actual subprocess calls
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

                clone_path = tmp_path / "test_clone"
                result = manager.create_clone(
                    clone_path=clone_path,
                    branch_name="feature-branch",
                    base_branch="main",
                    shallow=True,
                )

                assert result.success is True
                # Should create branch if different
                assert mock_run.call_count >= 1
                # Search call_args_list for the checkout call instead of relying on index
                checkout_calls = [c for c in mock_run.call_args_list if "checkout" in c[0][0]]
                assert len(checkout_calls) >= 1, "Expected a checkout call"
                cmd = checkout_calls[0][0][0]
                assert "-b" in cmd
                assert "feature-branch" in cmd

    def test_create_full_clone_success(self, manager, mock_run, tmp_path: Path) -> None:
        """Create clone executes full clone when shallow=False."""
        with patch.object(
            manager, "get_remote_url", return_value="https://github.com/user/repo.git"
        ):
            with patch.object(manager, "full_clone") as mock_full:
                mock_full.return_value = MagicMock(success=True, output="Cloned")

                clone_path = tmp_path / "test_clone"
                result = manager.create_clone(
                    clone_path=clone_path,
                    branch_name="main",  # Same branch, so no checkout -b
                    base_branch="main",
                    shallow=False,
                )

                assert result.success is True
                mock_full.assert_called_once()
                # Should NOT call checkout -b since branches match
                mock_run.assert_not_called()


class TestCloneGitManagerMergeBranch:
    """Tests for CloneGitManager.merge_branch method."""

    @pytest.fixture
    def manager(self, tmp_path: Path) -> CloneGitManager:
        """Create manager with temp directory as repo path."""
        from gobby.clones.git import CloneGitManager

        return CloneGitManager(repo_path=tmp_path)

    @pytest.fixture
    def mock_run(self):
        """Mock subprocess.run."""
        with patch("subprocess.run") as mock:
            yield mock

    def test_merge_branch_success(self, manager, mock_run, tmp_path: Path) -> None:
        """Merge branch succeeds when no conflicts."""
        # Sequence: rev-parse, fetch, checkout, pull, merge, checkout-restore
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="dev\n"),  # rev-parse (save branch)
            MagicMock(returncode=0),  # fetch
            MagicMock(returncode=0),  # checkout target
            MagicMock(returncode=0),  # pull
            MagicMock(returncode=0, stdout="Merged", stderr=""),  # merge
            MagicMock(returncode=0),  # checkout restore (finally)
        ]

        result = manager.merge_branch(
            source_branch="feature", target_branch="main", working_dir=tmp_path
        )

        assert result.success is True
        assert mock_run.call_count == 6

    def test_merge_branch_conflict(self, manager, mock_run, tmp_path: Path) -> None:
        """Merge branch handles conflicts and restores original branch."""
        # Sequence: rev-parse, fetch, checkout, pull, merge (fail), diff, abort, checkout-restore
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="dev\n"),  # rev-parse (save branch)
            MagicMock(returncode=0),  # fetch
            MagicMock(returncode=0),  # checkout target
            MagicMock(returncode=0),  # pull
            MagicMock(
                returncode=1, stdout="CONFLICT", stderr="Automatic merge failed"
            ),  # merge fail
            MagicMock(returncode=0, stdout="file.txt\n", stderr=""),  # diff U
            MagicMock(returncode=0),  # merge --abort
            MagicMock(returncode=0),  # checkout restore (finally)
        ]

        result = manager.merge_branch(
            source_branch="feature", target_branch="main", working_dir=tmp_path
        )

        assert result.success is False
        assert "conflict" in result.message.lower()
        assert "file.txt" in result.output
