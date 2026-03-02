"""Tests for git worktree operations manager."""

import subprocess
from unittest.mock import patch

import pytest

from gobby.worktrees.git import (
    GitOperationResult,
    WorktreeGitManager,
    WorktreeInfo,
    WorktreeStatus,
)

pytestmark = pytest.mark.unit


class TestWorktreeInfo:
    """Tests for WorktreeInfo dataclass."""

    def test_create_minimal(self) -> None:
        """WorktreeInfo can be created with required fields."""
        info = WorktreeInfo(
            path="/path/to/worktree",
            branch="feature/test",
            commit="abc1234",
        )

        assert info.path == "/path/to/worktree"
        assert info.branch == "feature/test"
        assert info.commit == "abc1234"
        assert info.is_bare is False
        assert info.is_detached is False
        assert info.locked is False
        assert info.prunable is False

    def test_create_with_all_fields(self) -> None:
        """WorktreeInfo can be created with all fields."""
        info = WorktreeInfo(
            path="/path/to/worktree",
            branch=None,
            commit="abc1234",
            is_bare=True,
            is_detached=True,
            locked=True,
            prunable=True,
        )

        assert info.is_bare is True
        assert info.is_detached is True
        assert info.locked is True
        assert info.prunable is True


class TestWorktreeStatus:
    """Tests for WorktreeStatus dataclass."""

    def test_create(self) -> None:
        """WorktreeStatus can be created with all fields."""
        status = WorktreeStatus(
            has_uncommitted_changes=True,
            has_staged_changes=True,
            has_untracked_files=True,
            ahead=5,
            behind=2,
            branch="feature/test",
            commit="abc1234",
        )

        assert status.has_uncommitted_changes is True
        assert status.has_staged_changes is True
        assert status.has_untracked_files is True
        assert status.ahead == 5
        assert status.behind == 2
        assert status.branch == "feature/test"
        assert status.commit == "abc1234"

    def test_clean_status(self) -> None:
        """WorktreeStatus can represent clean working tree."""
        status = WorktreeStatus(
            has_uncommitted_changes=False,
            has_staged_changes=False,
            has_untracked_files=False,
            ahead=0,
            behind=0,
            branch="main",
            commit="def5678",
        )

        assert status.has_uncommitted_changes is False
        assert status.has_staged_changes is False
        assert status.has_untracked_files is False


class TestGitOperationResult:
    """Tests for GitOperationResult dataclass."""

    def test_success_result(self) -> None:
        """GitOperationResult can represent success."""
        result = GitOperationResult(
            success=True,
            message="Operation completed",
            output="some output",
        )

        assert result.success is True
        assert result.message == "Operation completed"
        assert result.output == "some output"
        assert result.error is None

    def test_failure_result(self) -> None:
        """GitOperationResult can represent failure."""
        result = GitOperationResult(
            success=False,
            message="Operation failed",
            error="error details",
        )

        assert result.success is False
        assert result.message == "Operation failed"
        assert result.output is None
        assert result.error == "error details"


class TestWorktreeGitManagerInit:
    """Tests for WorktreeGitManager initialization."""

    def test_init_with_valid_path(self, tmp_path) -> None:
        """Manager initializes with valid path."""
        manager = WorktreeGitManager(tmp_path)

        assert manager.repo_path == tmp_path

    def test_init_with_string_path(self, tmp_path) -> None:
        """Manager accepts string path."""
        manager = WorktreeGitManager(str(tmp_path))

        assert manager.repo_path == tmp_path

    def test_init_invalid_path_raises(self) -> None:
        """Manager raises ValueError for non-existent path."""
        with pytest.raises(ValueError, match="does not exist"):
            WorktreeGitManager("/nonexistent/path")


class TestWorktreeGitManagerRunGit:
    """Tests for WorktreeGitManager._run_git method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_run_git_success(self, mock_run, manager) -> None:
        """_run_git returns CompletedProcess on success."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="On branch main",
            stderr="",
        )

        result = manager._run_git(["status"])

        assert result.returncode == 0
        assert result.stdout == "On branch main"
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_run_git_uses_repo_path(self, mock_run, manager) -> None:
        """_run_git uses repo_path as default cwd."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="",
            stderr="",
        )

        manager._run_git(["status"])

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["cwd"] == manager.repo_path

    @patch("subprocess.run")
    def test_run_git_custom_cwd(self, mock_run, manager, tmp_path) -> None:
        """_run_git accepts custom cwd."""
        custom_path = tmp_path / "custom"
        custom_path.mkdir()
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="",
            stderr="",
        )

        manager._run_git(["status"], cwd=custom_path)

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["cwd"] == custom_path

    @patch("subprocess.run")
    def test_run_git_timeout(self, mock_run, manager) -> None:
        """_run_git raises on timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

        with pytest.raises(subprocess.TimeoutExpired):
            manager._run_git(["status"])


class TestWorktreeGitManagerCreateWorktree:
    """Tests for WorktreeGitManager.create_worktree method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    def test_create_fails_if_path_exists(self, manager, tmp_path) -> None:
        """Create fails if worktree path already exists."""
        existing_path = tmp_path / "existing"
        existing_path.mkdir()

        result = manager.create_worktree(existing_path, "feature/test")

        assert result.success is False
        assert "already exists" in result.message

    @patch("subprocess.run")
    def test_create_with_new_branch(self, mock_run, manager, tmp_path) -> None:
        """Create worktree with new branch."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "add"],
            returncode=0,
            stdout="Preparing worktree",
            stderr="",
        )

        result = manager.create_worktree(
            worktree_path, "feature/test", base_branch="main", create_branch=True
        )

        assert result.success is True
        assert "Created worktree" in result.message

    @patch("subprocess.run")
    def test_create_with_existing_branch(self, mock_run, manager, tmp_path) -> None:
        """Create worktree with existing branch."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "add"],
            returncode=0,
            stdout="Preparing worktree",
            stderr="",
        )

        result = manager.create_worktree(worktree_path, "feature/test", create_branch=False)

        assert result.success is True

    @patch("subprocess.run")
    def test_create_handles_git_failure(self, mock_run, manager, tmp_path) -> None:
        """Create handles git command failure."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        # First call is fetch (succeeds), second call is worktree add (fails)
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["git", "fetch"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "worktree", "add"],
                returncode=128,
                stdout="",
                stderr="fatal: branch already exists",
            ),
        ]

        result = manager.create_worktree(worktree_path, "feature/test")

        assert result.success is False
        assert "Failed to create" in result.message

    @patch("subprocess.run")
    def test_create_handles_timeout(self, mock_run, manager, tmp_path) -> None:
        """Create handles git timeout."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=60)

        result = manager.create_worktree(worktree_path, "feature/test")

        assert result.success is False
        assert "timed out" in result.message


class TestWorktreeGitManagerDeleteWorktree:
    """Tests for WorktreeGitManager.delete_worktree method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_delete_success(self, mock_run, manager, tmp_path) -> None:
        """Delete worktree successfully."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "remove"],
            returncode=0,
            stdout="",
            stderr="",
        )

        result = manager.delete_worktree(worktree_path)

        assert result.success is True
        assert "Deleted worktree" in result.message

    @patch("subprocess.run")
    def test_delete_with_force(self, mock_run, manager, tmp_path) -> None:
        """Delete worktree with force option."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "remove", "--force"],
            returncode=0,
            stdout="",
            stderr="",
        )

        result = manager.delete_worktree(worktree_path, force=True)

        assert result.success is True
        # Check that --force was passed
        call_args = mock_run.call_args[0][0]
        assert "--force" in call_args

    @patch("subprocess.run")
    def test_delete_with_branch_deletion(self, mock_run, manager, tmp_path) -> None:
        """Delete worktree and associated branch."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        worktree_path.mkdir(parents=True)

        # Mock sequence: status check, worktree remove, branch delete
        mock_run.side_effect = [
            # branch --show-current
            subprocess.CompletedProcess(
                args=["git", "branch"], returncode=0, stdout="feature/test\n", stderr=""
            ),
            # rev-parse --short HEAD
            subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=0, stdout="abc1234\n", stderr=""
            ),
            # status --porcelain
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            # rev-list (ahead/behind)
            subprocess.CompletedProcess(
                args=["git", "rev-list"], returncode=0, stdout="0\t0\n", stderr=""
            ),
            # worktree remove
            subprocess.CompletedProcess(
                args=["git", "worktree", "remove"], returncode=0, stdout="", stderr=""
            ),
            # branch -d
            subprocess.CompletedProcess(
                args=["git", "branch", "-d"], returncode=0, stdout="", stderr=""
            ),
        ]

        result = manager.delete_worktree(worktree_path, delete_branch=True)

        assert result.success is True
        assert "branch" in result.message

    @patch("subprocess.run")
    def test_delete_handles_failure(self, mock_run, manager, tmp_path) -> None:
        """Delete handles git failure."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "remove"],
            returncode=128,
            stdout="",
            stderr="error: cannot remove: dirty",
        )

        result = manager.delete_worktree(worktree_path)

        assert result.success is False
        assert "Failed to remove" in result.message

    @patch("subprocess.run")
    def test_delete_force_fallback_on_untracked_files(self, mock_run, manager, tmp_path) -> None:
        """Force delete falls back to rmtree when git remove fails with untracked files."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        worktree_path.mkdir(parents=True)
        # Simulate untracked files that cause git worktree remove to fail
        (worktree_path / ".mypy_cache").mkdir()
        (worktree_path / ".mypy_cache" / "data.json").write_text("{}")

        # git worktree remove --force fails, then git worktree prune succeeds
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["git", "worktree", "remove", "--force"],
                returncode=128,
                stdout="",
                stderr="fatal: cannot remove: Directory not empty",
            ),
            subprocess.CompletedProcess(
                args=["git", "worktree", "prune"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        ]

        result = manager.delete_worktree(worktree_path, force=True)

        assert result.success is True
        assert not worktree_path.exists()


class TestWorktreeGitManagerSyncFromMain:
    """Tests for WorktreeGitManager.sync_from_main method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    def test_sync_fails_if_path_not_exists(self, manager, tmp_path) -> None:
        """Sync fails if worktree path doesn't exist."""
        worktree_path = tmp_path / "nonexistent"

        result = manager.sync_from_main(worktree_path)

        assert result.success is False
        assert "does not exist" in result.message

    @patch("subprocess.run")
    def test_sync_with_rebase(self, mock_run, manager, tmp_path) -> None:
        """Sync with rebase strategy."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # fetch
            subprocess.CompletedProcess(args=["git", "fetch"], returncode=0, stdout="", stderr=""),
            # rebase
            subprocess.CompletedProcess(args=["git", "rebase"], returncode=0, stdout="", stderr=""),
        ]

        result = manager.sync_from_main(worktree_path, strategy="rebase")

        assert result.success is True
        assert "rebase" in result.message

    @patch("subprocess.run")
    def test_sync_with_merge(self, mock_run, manager, tmp_path) -> None:
        """Sync with merge strategy."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # fetch
            subprocess.CompletedProcess(args=["git", "fetch"], returncode=0, stdout="", stderr=""),
            # merge
            subprocess.CompletedProcess(args=["git", "merge"], returncode=0, stdout="", stderr=""),
        ]

        result = manager.sync_from_main(worktree_path, strategy="merge")

        assert result.success is True
        assert "merge" in result.message

    @patch("subprocess.run")
    def test_sync_handles_conflict(self, mock_run, manager, tmp_path) -> None:
        """Sync handles merge/rebase conflicts."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # fetch
            subprocess.CompletedProcess(args=["git", "fetch"], returncode=0, stdout="", stderr=""),
            # rebase with conflict
            subprocess.CompletedProcess(
                args=["git", "rebase"],
                returncode=1,
                stdout="CONFLICT (content): ...",
                stderr="",
            ),
        ]

        result = manager.sync_from_main(worktree_path)

        assert result.success is False
        assert "conflicts" in result.message.lower()

    @patch("subprocess.run")
    def test_sync_handles_fetch_failure(self, mock_run, manager, tmp_path) -> None:
        """Sync handles fetch failure."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "fetch"],
            returncode=128,
            stdout="",
            stderr="fatal: could not fetch",
        )

        result = manager.sync_from_main(worktree_path)

        assert result.success is False
        assert "Failed to fetch" in result.message


class TestWorktreeGitManagerGetStatus:
    """Tests for WorktreeGitManager.get_worktree_status method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    def test_get_status_nonexistent_path(self, manager, tmp_path) -> None:
        """Get status returns None for non-existent path."""
        result = manager.get_worktree_status(tmp_path / "nonexistent")

        assert result is None

    @patch("subprocess.run")
    def test_get_status_clean(self, mock_run, manager, tmp_path) -> None:
        """Get status for clean worktree."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # branch --show-current
            subprocess.CompletedProcess(
                args=["git", "branch"], returncode=0, stdout="main\n", stderr=""
            ),
            # rev-parse --short HEAD
            subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=0, stdout="abc1234\n", stderr=""
            ),
            # status --porcelain
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            # rev-list (ahead/behind)
            subprocess.CompletedProcess(
                args=["git", "rev-list"], returncode=0, stdout="0\t0\n", stderr=""
            ),
        ]

        status = manager.get_worktree_status(worktree_path)

        assert status is not None
        assert status.branch == "main"
        assert status.commit == "abc1234"
        assert status.has_uncommitted_changes is False
        assert status.has_staged_changes is False
        assert status.has_untracked_files is False
        assert status.ahead == 0
        assert status.behind == 0

    @patch("subprocess.run")
    def test_get_status_with_changes(self, mock_run, manager, tmp_path) -> None:
        """Get status for worktree with changes."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # branch --show-current
            subprocess.CompletedProcess(
                args=["git", "branch"], returncode=0, stdout="feature/test\n", stderr=""
            ),
            # rev-parse --short HEAD
            subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=0, stdout="def5678\n", stderr=""
            ),
            # status --porcelain (staged, modified, untracked)
            subprocess.CompletedProcess(
                args=["git", "status"],
                returncode=0,
                stdout="M  staged.py\n M modified.py\n?? untracked.py\n",
                stderr="",
            ),
            # rev-list (ahead/behind)
            subprocess.CompletedProcess(
                args=["git", "rev-list"], returncode=0, stdout="2\t3\n", stderr=""
            ),
        ]

        status = manager.get_worktree_status(worktree_path)

        assert status is not None
        assert status.has_staged_changes is True
        assert status.has_uncommitted_changes is True
        assert status.has_untracked_files is True
        assert status.behind == 2
        assert status.ahead == 3

    @patch("subprocess.run")
    def test_get_status_handles_exception(self, mock_run, manager, tmp_path) -> None:
        """Get status handles exception gracefully."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = Exception("Git error")

        status = manager.get_worktree_status(worktree_path)

        assert status is None


class TestWorktreeGitManagerListWorktrees:
    """Tests for WorktreeGitManager.list_worktrees method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_list_empty(self, mock_run, manager) -> None:
        """List returns empty list when no worktrees."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=0,
            stdout="",
            stderr="",
        )

        worktrees = manager.list_worktrees()

        assert worktrees == []

    @patch("subprocess.run")
    def test_list_single_worktree(self, mock_run, manager) -> None:
        """List returns single worktree."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=0,
            stdout="worktree /path/to/repo\nHEAD abc1234567890\nbranch refs/heads/main\n\n",
            stderr="",
        )

        worktrees = manager.list_worktrees()

        assert len(worktrees) == 1
        assert worktrees[0].path == "/path/to/repo"
        assert worktrees[0].commit == "abc1234567890"
        assert worktrees[0].branch == "main"

    @patch("subprocess.run")
    def test_list_multiple_worktrees(self, mock_run, manager) -> None:
        """List returns multiple worktrees."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=0,
            stdout=(
                "worktree /path/to/repo\n"
                "HEAD abc1234567890\n"
                "branch refs/heads/main\n"
                "\n"
                "worktree /path/to/worktree1\n"
                "HEAD def5678901234\n"
                "branch refs/heads/feature/one\n"
                "\n"
            ),
            stderr="",
        )

        worktrees = manager.list_worktrees()

        assert len(worktrees) == 2
        assert worktrees[0].branch == "main"
        assert worktrees[1].branch == "feature/one"

    @patch("subprocess.run")
    def test_list_with_flags(self, mock_run, manager) -> None:
        """List parses locked/prunable/detached flags."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=0,
            stdout=(
                "worktree /path/to/worktree\nHEAD abc1234567890\ndetached\nlocked\nprunable\n\n"
            ),
            stderr="",
        )

        worktrees = manager.list_worktrees()

        assert len(worktrees) == 1
        assert worktrees[0].is_detached is True
        assert worktrees[0].locked is True
        assert worktrees[0].prunable is True
        assert worktrees[0].branch is None

    @patch("subprocess.run")
    def test_list_handles_failure(self, mock_run, manager) -> None:
        """List returns empty list on git failure."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository",
        )

        worktrees = manager.list_worktrees()

        assert worktrees == []


class TestWorktreeGitManagerPrune:
    """Tests for WorktreeGitManager.prune_worktrees method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_prune_success(self, mock_run, manager) -> None:
        """Prune succeeds."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "prune"],
            returncode=0,
            stdout="",
            stderr="",
        )

        result = manager.prune_worktrees()

        assert result.success is True
        assert "Pruned" in result.message

    @patch("subprocess.run")
    def test_prune_failure(self, mock_run, manager) -> None:
        """Prune handles failure."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "prune"],
            returncode=1,
            stdout="",
            stderr="error: pruning failed",
        )

        result = manager.prune_worktrees()

        assert result.success is False
        assert "Failed to prune" in result.message


class TestWorktreeGitManagerLock:
    """Tests for WorktreeGitManager.lock_worktree method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_lock_success(self, mock_run, manager, tmp_path) -> None:
        """Lock worktree successfully."""
        worktree_path = tmp_path / "worktree"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "lock"],
            returncode=0,
            stdout="",
            stderr="",
        )

        result = manager.lock_worktree(worktree_path)

        assert result.success is True
        assert "Locked" in result.message

    @patch("subprocess.run")
    def test_lock_with_reason(self, mock_run, manager, tmp_path) -> None:
        """Lock worktree with reason."""
        worktree_path = tmp_path / "worktree"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "lock"],
            returncode=0,
            stdout="",
            stderr="",
        )

        result = manager.lock_worktree(worktree_path, reason="Important work")

        assert result.success is True
        # Check that --reason was passed
        call_args = mock_run.call_args[0][0]
        assert "--reason" in call_args
        assert "Important work" in call_args

    @patch("subprocess.run")
    def test_lock_failure(self, mock_run, manager, tmp_path) -> None:
        """Lock handles failure."""
        worktree_path = tmp_path / "worktree"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "lock"],
            returncode=128,
            stdout="",
            stderr="error: already locked",
        )

        result = manager.lock_worktree(worktree_path)

        assert result.success is False
        assert "Failed to lock" in result.message


class TestWorktreeGitManagerUnlock:
    """Tests for WorktreeGitManager.unlock_worktree method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_unlock_success(self, mock_run, manager, tmp_path) -> None:
        """Unlock worktree successfully."""
        worktree_path = tmp_path / "worktree"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "unlock"],
            returncode=0,
            stdout="",
            stderr="",
        )

        result = manager.unlock_worktree(worktree_path)

        assert result.success is True
        assert "Unlocked" in result.message

    @patch("subprocess.run")
    def test_unlock_failure(self, mock_run, manager, tmp_path) -> None:
        """Unlock handles failure."""
        worktree_path = tmp_path / "worktree"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "unlock"],
            returncode=128,
            stdout="",
            stderr="error: not locked",
        )

        result = manager.unlock_worktree(worktree_path)

        assert result.success is False
        assert "Failed to unlock" in result.message

    @patch("subprocess.run")
    def test_unlock_handles_exception(self, mock_run, manager, tmp_path) -> None:
        """Unlock handles generic exception gracefully."""
        worktree_path = tmp_path / "worktree"
        mock_run.side_effect = Exception("Unexpected error")

        result = manager.unlock_worktree(worktree_path)

        assert result.success is False
        assert "Error unlocking worktree" in result.message
        assert result.error == "Unexpected error"


class TestWorktreeGitManagerRunGitCalledProcessError:
    """Tests for _run_git CalledProcessError handling."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_run_git_called_process_error(self, mock_run, manager) -> None:
        """_run_git raises CalledProcessError when check=True."""
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=128,
            cmd=["git", "status"],
            stderr="fatal: not a git repository",
        )

        with pytest.raises(subprocess.CalledProcessError):
            manager._run_git(["status"], check=True)


class TestWorktreeGitManagerCreateWorktreeFetchFailure:
    """Tests for create_worktree fetch failure scenarios."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_create_worktree_fetch_failure(self, mock_run, manager, tmp_path) -> None:
        """Create worktree fails when fetch fails."""
        worktree_path = tmp_path / "worktrees" / "feature-test"

        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "fetch"],
            returncode=128,
            stdout="",
            stderr="fatal: could not fetch origin/main",
        )

        result = manager.create_worktree(
            worktree_path, "feature/test", base_branch="main", create_branch=True
        )

        assert result.success is False
        assert "Failed to fetch" in result.message
        assert result.error == "fatal: could not fetch origin/main"

    @patch("subprocess.run")
    def test_create_worktree_generic_exception(self, mock_run, manager, tmp_path) -> None:
        """Create worktree handles generic exception."""
        worktree_path = tmp_path / "worktrees" / "feature-test"

        mock_run.side_effect = Exception("Unexpected git error")

        result = manager.create_worktree(
            worktree_path, "feature/test", base_branch="main", create_branch=True
        )

        assert result.success is False
        assert "Error creating worktree" in result.message
        assert result.error == "Unexpected git error"


class TestWorktreeGitManagerDeleteWorktreeEdgeCases:
    """Tests for delete_worktree edge cases."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_delete_branch_deletion_failure(self, mock_run, manager, tmp_path) -> None:
        """Delete worktree succeeds but branch deletion fails."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        worktree_path.mkdir(parents=True)

        # Mock sequence: get status, worktree remove success, branch delete fails
        mock_run.side_effect = [
            # branch --show-current
            subprocess.CompletedProcess(
                args=["git", "branch"], returncode=0, stdout="feature/test\n", stderr=""
            ),
            # rev-parse --short HEAD
            subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=0, stdout="abc1234\n", stderr=""
            ),
            # status --porcelain
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            # rev-list (ahead/behind)
            subprocess.CompletedProcess(
                args=["git", "rev-list"], returncode=0, stdout="0\t0\n", stderr=""
            ),
            # worktree remove - success
            subprocess.CompletedProcess(
                args=["git", "worktree", "remove"], returncode=0, stdout="", stderr=""
            ),
            # branch -d - failure (not fully merged)
            subprocess.CompletedProcess(
                args=["git", "branch", "-d"],
                returncode=1,
                stdout="",
                stderr="error: branch not fully merged",
            ),
        ]

        result = manager.delete_worktree(worktree_path, delete_branch=True)

        # Worktree was removed, so success is True, but message indicates branch issue
        assert result.success is True
        assert "failed to delete branch" in result.message

    @patch("subprocess.run")
    def test_delete_branch_with_no_status(self, mock_run, manager, tmp_path) -> None:
        """Delete worktree with delete_branch=True but no status found."""
        worktree_path = tmp_path / "worktrees" / "feature-test"
        # Path doesn't exist, so get_worktree_status returns None

        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "remove"], returncode=0, stdout="", stderr=""
        )

        result = manager.delete_worktree(worktree_path, delete_branch=True)

        assert result.success is True
        # No branch was deleted since we couldn't determine the branch
        assert "branch" not in result.message.lower() or "and branch" not in result.message
        # Strictly verify we didn't try to delete a branch
        assert (
            "Deleted worktree" in result.message and "deleted branch" not in result.message.lower()
        )

    @patch("subprocess.run")
    def test_delete_timeout(self, mock_run, manager, tmp_path) -> None:
        """Delete worktree handles timeout."""
        worktree_path = tmp_path / "worktrees" / "feature-test"

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=30)

        result = manager.delete_worktree(worktree_path)

        assert result.success is False
        assert "timed out" in result.message

    @patch("subprocess.run")
    def test_delete_generic_exception(self, mock_run, manager, tmp_path) -> None:
        """Delete worktree handles generic exception."""
        worktree_path = tmp_path / "worktrees" / "feature-test"

        mock_run.side_effect = Exception("Unexpected error during delete")

        result = manager.delete_worktree(worktree_path)

        assert result.success is False
        assert "Error deleting worktree" in result.message
        assert result.error == "Unexpected error during delete"


class TestWorktreeGitManagerSyncEdgeCases:
    """Tests for sync_from_main edge cases."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_sync_rebase_failure_no_conflict(self, mock_run, manager, tmp_path) -> None:
        """Sync fails with rebase error but no conflict."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # fetch success
            subprocess.CompletedProcess(args=["git", "fetch"], returncode=0, stdout="", stderr=""),
            # rebase failure (not a conflict)
            subprocess.CompletedProcess(
                args=["git", "rebase"],
                returncode=1,
                stdout="",
                stderr="error: cannot rebase: dirty index",
            ),
        ]

        result = manager.sync_from_main(worktree_path)

        assert result.success is False
        assert "Failed to rebase" in result.message
        assert "dirty index" in result.error

    @patch("subprocess.run")
    def test_sync_merge_failure_no_conflict(self, mock_run, manager, tmp_path) -> None:
        """Sync fails with merge error but no conflict."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # fetch success
            subprocess.CompletedProcess(args=["git", "fetch"], returncode=0, stdout="", stderr=""),
            # merge failure (not a conflict)
            subprocess.CompletedProcess(
                args=["git", "merge"],
                returncode=1,
                stdout="",
                stderr="error: You have unstaged changes",
            ),
        ]

        result = manager.sync_from_main(worktree_path, strategy="merge")

        assert result.success is False
        assert "Failed to merge" in result.message

    @patch("subprocess.run")
    def test_sync_conflict_in_stderr(self, mock_run, manager, tmp_path) -> None:
        """Sync detects conflict in stderr."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # fetch success
            subprocess.CompletedProcess(args=["git", "fetch"], returncode=0, stdout="", stderr=""),
            # merge with conflict in stderr
            subprocess.CompletedProcess(
                args=["git", "merge"],
                returncode=1,
                stdout="",
                stderr="CONFLICT (content): Merge conflict in file.py",
            ),
        ]

        result = manager.sync_from_main(worktree_path, strategy="merge")

        assert result.success is False
        assert "conflicts" in result.message.lower()
        assert "abort" in result.message.lower()

    @patch("subprocess.run")
    def test_sync_timeout(self, mock_run, manager, tmp_path) -> None:
        """Sync handles timeout."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=120)

        result = manager.sync_from_main(worktree_path)

        assert result.success is False
        assert "timed out" in result.message

    @patch("subprocess.run")
    def test_sync_generic_exception(self, mock_run, manager, tmp_path) -> None:
        """Sync handles generic exception."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = Exception("Network error")

        result = manager.sync_from_main(worktree_path)

        assert result.success is False
        assert "Error syncing worktree" in result.message
        assert result.error == "Network error"


class TestWorktreeGitManagerGetStatusEdgeCases:
    """Tests for get_worktree_status edge cases."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_get_status_no_upstream(self, mock_run, manager, tmp_path) -> None:
        """Get status when branch has no upstream."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # branch --show-current
            subprocess.CompletedProcess(
                args=["git", "branch"], returncode=0, stdout="feature/test\n", stderr=""
            ),
            # rev-parse --short HEAD
            subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=0, stdout="abc1234\n", stderr=""
            ),
            # status --porcelain (clean)
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            # rev-list fails (no upstream)
            subprocess.CompletedProcess(
                args=["git", "rev-list"],
                returncode=128,
                stdout="",
                stderr="fatal: no upstream branch",
            ),
        ]

        status = manager.get_worktree_status(worktree_path)

        assert status is not None
        # Without upstream, ahead/behind defaults to 0
        assert status.ahead == 0
        assert status.behind == 0

    @patch("subprocess.run")
    def test_get_status_detached_head(self, mock_run, manager, tmp_path) -> None:
        """Get status with detached HEAD (no branch)."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # branch --show-current returns empty (detached)
            subprocess.CompletedProcess(args=["git", "branch"], returncode=0, stdout="", stderr=""),
            # rev-parse --short HEAD
            subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=0, stdout="abc1234\n", stderr=""
            ),
            # status --porcelain (clean)
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            # No rev-list call since branch is empty
        ]

        status = manager.get_worktree_status(worktree_path)

        assert status is not None
        assert status.branch == ""
        assert status.commit == "abc1234"
        # Without branch, upstream check is skipped
        assert status.ahead == 0
        assert status.behind == 0

    @patch("subprocess.run")
    def test_get_status_branch_command_failure(self, mock_run, manager, tmp_path) -> None:
        """Get status when branch command fails."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # branch --show-current fails
            subprocess.CompletedProcess(
                args=["git", "branch"],
                returncode=128,
                stdout="",
                stderr="fatal: not a git repo",
            ),
            # rev-parse fails
            subprocess.CompletedProcess(
                args=["git", "rev-parse"],
                returncode=128,
                stdout="",
                stderr="fatal: not a git repo",
            ),
            # status --porcelain
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
        ]

        status = manager.get_worktree_status(worktree_path)

        assert status is not None
        assert status.branch is None
        assert status.commit is None

    @patch("subprocess.run")
    def test_get_status_ahead_behind_parsing(self, mock_run, manager, tmp_path) -> None:
        """Get status parses ahead/behind correctly."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # branch --show-current
            subprocess.CompletedProcess(
                args=["git", "branch"], returncode=0, stdout="main\n", stderr=""
            ),
            # rev-parse --short HEAD
            subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=0, stdout="abc1234\n", stderr=""
            ),
            # status --porcelain
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            # rev-list with single tab (malformed output) - should not crash
            subprocess.CompletedProcess(
                args=["git", "rev-list"], returncode=0, stdout="5\t", stderr=""
            ),
        ]

        status = manager.get_worktree_status(worktree_path)

        assert status is not None
        # With malformed output, should not parse correctly
        assert status.ahead == 0
        assert status.behind == 0

    @patch("subprocess.run")
    def test_get_status_status_porcelain_parsing(self, mock_run, manager, tmp_path) -> None:
        """Get status correctly parses various porcelain status formats."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # branch --show-current
            subprocess.CompletedProcess(
                args=["git", "branch"], returncode=0, stdout="main\n", stderr=""
            ),
            # rev-parse --short HEAD
            subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=0, stdout="abc1234\n", stderr=""
            ),
            # status --porcelain with various statuses:
            # A  = staged new file
            # AM = staged new file with modifications
            # MM = staged and modified
            # D  = staged deletion
            #  D = deleted in worktree
            # ?? = untracked
            subprocess.CompletedProcess(
                args=["git", "status"],
                returncode=0,
                stdout="A  new_file.py\nAM modified_staged.py\nMM both.py\nD  deleted.py\n D removed.py\n?? untracked.txt\n",
                stderr="",
            ),
            # rev-list (no upstream)
            subprocess.CompletedProcess(
                args=["git", "rev-list"], returncode=128, stdout="", stderr=""
            ),
        ]

        status = manager.get_worktree_status(worktree_path)

        assert status is not None
        assert status.has_staged_changes is True
        assert status.has_uncommitted_changes is True
        assert status.has_untracked_files is True


class TestWorktreeGitManagerListWorktreesEdgeCases:
    """Tests for list_worktrees edge cases."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_list_worktrees_bare_repo(self, mock_run, manager) -> None:
        """List worktrees parses bare repository."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=0,
            stdout=("worktree /path/to/repo.git\nHEAD abc1234567890\nbare\n\n"),
            stderr="",
        )

        worktrees = manager.list_worktrees()

        assert len(worktrees) == 1
        assert worktrees[0].is_bare is True
        assert worktrees[0].path == "/path/to/repo.git"

    @patch("subprocess.run")
    def test_list_worktrees_non_refs_heads_branch(self, mock_run, manager) -> None:
        """List worktrees parses branches without refs/heads prefix."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=0,
            stdout=(
                "worktree /path/to/worktree\nHEAD abc1234567890\nbranch feature/direct-branch\n\n"
            ),
            stderr="",
        )

        worktrees = manager.list_worktrees()

        assert len(worktrees) == 1
        # Branch without refs/heads/ prefix should be used as-is
        assert worktrees[0].branch == "feature/direct-branch"

    @patch("subprocess.run")
    def test_list_worktrees_locked_with_reason(self, mock_run, manager) -> None:
        """List worktrees parses locked with reason."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=0,
            stdout=(
                "worktree /path/to/worktree\n"
                "HEAD abc1234567890\n"
                "branch refs/heads/feature\n"
                "locked reason: important work\n"
                "\n"
            ),
            stderr="",
        )

        worktrees = manager.list_worktrees()

        assert len(worktrees) == 1
        assert worktrees[0].locked is True

    @patch("subprocess.run")
    def test_list_worktrees_prunable_with_reason(self, mock_run, manager) -> None:
        """List worktrees parses prunable with reason."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=0,
            stdout=(
                "worktree /path/to/worktree\n"
                "HEAD abc1234567890\n"
                "branch refs/heads/feature\n"
                "prunable gitdir file points to non-existent location\n"
                "\n"
            ),
            stderr="",
        )

        worktrees = manager.list_worktrees()

        assert len(worktrees) == 1
        assert worktrees[0].prunable is True

    @patch("subprocess.run")
    def test_list_worktrees_exception(self, mock_run, manager) -> None:
        """List worktrees handles exception gracefully."""
        mock_run.side_effect = Exception("Git process crashed")

        worktrees = manager.list_worktrees()

        assert worktrees == []

    @patch("subprocess.run")
    def test_list_worktrees_no_trailing_newline(self, mock_run, manager) -> None:
        """List worktrees handles output without trailing newline."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=0,
            stdout="worktree /path/to/repo\nHEAD abc1234567890\nbranch refs/heads/main",
            stderr="",
        )

        worktrees = manager.list_worktrees()

        # Should handle last entry without trailing newline
        assert len(worktrees) == 1
        assert worktrees[0].path == "/path/to/repo"
        assert worktrees[0].branch == "main"


class TestWorktreeGitManagerPruneEdgeCases:
    """Tests for prune_worktrees edge cases."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_prune_exception(self, mock_run, manager) -> None:
        """Prune handles exception gracefully."""
        mock_run.side_effect = Exception("Git process crashed")

        result = manager.prune_worktrees()

        assert result.success is False
        assert "Error pruning worktrees" in result.message
        assert result.error == "Git process crashed"


class TestWorktreeGitManagerLockEdgeCases:
    """Tests for lock_worktree edge cases."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_lock_exception(self, mock_run, manager, tmp_path) -> None:
        """Lock handles exception gracefully."""
        worktree_path = tmp_path / "worktree"
        mock_run.side_effect = Exception("Permission denied")

        result = manager.lock_worktree(worktree_path)

        assert result.success is False
        assert "Error locking worktree" in result.message
        assert result.error == "Permission denied"


class TestWorktreeGitManagerBranchCoverage:
    """Tests specifically for branch coverage gaps."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    @patch("subprocess.run")
    def test_get_status_porcelain_failure(self, mock_run, manager, tmp_path) -> None:
        """Get status when status --porcelain command fails."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # branch --show-current
            subprocess.CompletedProcess(
                args=["git", "branch"], returncode=0, stdout="main\n", stderr=""
            ),
            # rev-parse --short HEAD
            subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=0, stdout="abc1234\n", stderr=""
            ),
            # status --porcelain fails
            subprocess.CompletedProcess(
                args=["git", "status"],
                returncode=128,
                stdout="",
                stderr="fatal: not a git repository",
            ),
            # rev-list (ahead/behind)
            subprocess.CompletedProcess(
                args=["git", "rev-list"], returncode=0, stdout="0\t0\n", stderr=""
            ),
        ]

        status = manager.get_worktree_status(worktree_path)

        assert status is not None
        # When porcelain fails, flags should remain False (defaults)
        assert status.has_uncommitted_changes is False
        assert status.has_staged_changes is False
        assert status.has_untracked_files is False
        assert status.branch == "main"
        assert status.commit == "abc1234"

    @patch("subprocess.run")
    def test_list_worktrees_unknown_line_format(self, mock_run, manager) -> None:
        """List worktrees ignores unknown line formats."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list"],
            returncode=0,
            stdout=(
                "worktree /path/to/repo\n"
                "HEAD abc1234567890\n"
                "branch refs/heads/main\n"
                "unknown_field some_value\n"  # Unknown field
                "another_unknown\n"  # Another unknown
                "\n"
            ),
            stderr="",
        )

        worktrees = manager.list_worktrees()

        # Should still parse the worktree correctly, ignoring unknown fields
        assert len(worktrees) == 1
        assert worktrees[0].path == "/path/to/repo"
        assert worktrees[0].branch == "main"
        assert worktrees[0].commit == "abc1234567890"

    @patch("subprocess.run")
    def test_get_status_single_char_status_line(self, mock_run, manager, tmp_path) -> None:
        """Get status handles single character status line (edge case)."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        mock_run.side_effect = [
            # branch --show-current
            subprocess.CompletedProcess(
                args=["git", "branch"], returncode=0, stdout="main\n", stderr=""
            ),
            # rev-parse --short HEAD
            subprocess.CompletedProcess(
                args=["git", "rev-parse"], returncode=0, stdout="abc1234\n", stderr=""
            ),
            # status --porcelain with edge case single character line
            subprocess.CompletedProcess(
                args=["git", "status"],
                returncode=0,
                stdout="M\n",  # Single char line (malformed but should not crash)
                stderr="",
            ),
            # rev-list (ahead/behind)
            subprocess.CompletedProcess(
                args=["git", "rev-list"], returncode=0, stdout="0\t0\n", stderr=""
            ),
        ]

        status = manager.get_worktree_status(worktree_path)

        assert status is not None
        # Single char 'M' in index position means staged
        assert status.has_staged_changes is True
        # The line[1] access will return " " since there's only 1 char
        assert status.has_uncommitted_changes is False


@patch("gobby.worktrees.git.subprocess.run")
class TestWorktreeGitManagerGetDefaultBranch:
    """Tests for get_default_branch method."""

    def test_get_default_branch_from_origin_head(self, mock_run, tmp_path) -> None:
        """Get default branch from origin/HEAD symbolic ref."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        manager = WorktreeGitManager(repo_path)

        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "symbolic-ref"],
            returncode=0,
            stdout="refs/remotes/origin/main\n",
            stderr="",
        )

        branch = manager.get_default_branch()

        assert branch == "main"

    def test_get_default_branch_from_origin_head_master(self, mock_run, tmp_path) -> None:
        """Get default branch 'master' from origin/HEAD."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        manager = WorktreeGitManager(repo_path)

        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "symbolic-ref"],
            returncode=0,
            stdout="refs/remotes/origin/master\n",
            stderr="",
        )

        branch = manager.get_default_branch()

        assert branch == "master"

    def test_get_default_branch_fallback_to_local_main(self, mock_run, tmp_path) -> None:
        """Fall back to local main branch when origin/HEAD fails."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        manager = WorktreeGitManager(repo_path)

        mock_run.side_effect = [
            # symbolic-ref fails (no origin/HEAD)
            subprocess.CompletedProcess(
                args=["git", "symbolic-ref"],
                returncode=128,
                stdout="",
                stderr="fatal: ref refs/remotes/origin/HEAD is not a symbolic ref",
            ),
            # rev-parse for local main succeeds
            subprocess.CompletedProcess(
                args=["git", "rev-parse"],
                returncode=0,
                stdout="abc123\n",
                stderr="",
            ),
        ]

        branch = manager.get_default_branch()

        assert branch == "main"

    def test_get_default_branch_fallback_to_remote_master(self, mock_run, tmp_path) -> None:
        """Fall back to remote master when main doesn't exist."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        manager = WorktreeGitManager(repo_path)

        mock_run.side_effect = [
            # symbolic-ref fails
            subprocess.CompletedProcess(
                args=["git", "symbolic-ref"],
                returncode=128,
                stdout="",
                stderr="fatal: not a symbolic ref",
            ),
            # rev-parse for local main fails
            subprocess.CompletedProcess(
                args=["git", "rev-parse"],
                returncode=128,
                stdout="",
                stderr="fatal: Needed a single revision",
            ),
            # rev-parse for remote main fails
            subprocess.CompletedProcess(
                args=["git", "rev-parse"],
                returncode=128,
                stdout="",
                stderr="fatal: Needed a single revision",
            ),
            # rev-parse for local master fails
            subprocess.CompletedProcess(
                args=["git", "rev-parse"],
                returncode=128,
                stdout="",
                stderr="fatal: Needed a single revision",
            ),
            # rev-parse for remote master succeeds
            subprocess.CompletedProcess(
                args=["git", "rev-parse"],
                returncode=0,
                stdout="def456\n",
                stderr="",
            ),
        ]

        branch = manager.get_default_branch()

        assert branch == "master"

    def test_get_default_branch_fallback_to_main(self, mock_run, tmp_path) -> None:
        """Fall back to 'main' when all detection methods fail."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        manager = WorktreeGitManager(repo_path)

        # All git commands fail
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git"],
            returncode=128,
            stdout="",
            stderr="fatal: error",
        )

        branch = manager.get_default_branch()

        # Should fall back to "main"
        assert branch == "main"

    def test_get_default_branch_handles_exception(self, mock_run, tmp_path) -> None:
        """Handle exception gracefully and fall back to main."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        manager = WorktreeGitManager(repo_path)

        mock_run.side_effect = Exception("Git not available")

        branch = manager.get_default_branch()

        # Should fall back to "main"
        assert branch == "main"


class TestWorktreeGitManagerMergeBranch:
    """Tests for WorktreeGitManager.merge_branch method."""

    @pytest.fixture
    def manager(self, tmp_path):
        """Create manager with temp directory."""
        return WorktreeGitManager(tmp_path)

    def _mock_rev_parse(self, branch="feature/test"):
        """Helper: mock for initial rev-parse --abbrev-ref HEAD."""
        return subprocess.CompletedProcess(
            args=["git", "rev-parse"], returncode=0, stdout=f"{branch}\n", stderr=""
        )

    def _mock_success(self, args_hint="git"):
        """Helper: generic successful CompletedProcess."""
        return subprocess.CompletedProcess(args=[args_hint], returncode=0, stdout="", stderr="")

    def _mock_failure(self, stderr="error", args_hint="git"):
        """Helper: generic failed CompletedProcess."""
        return subprocess.CompletedProcess(args=[args_hint], returncode=1, stdout="", stderr=stderr)

    @patch("subprocess.run")
    def test_merge_success_with_push(self, mock_run, manager) -> None:
        """Merge succeeds with push."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            self._mock_success(),  # fetch origin
            self._mock_success(),  # checkout main
            self._mock_success(),  # pull origin main
            subprocess.CompletedProcess(
                args=["git", "merge"], returncode=0, stdout="Merge made by recursive\n", stderr=""
            ),  # merge --no-ff
            self._mock_success(),  # push origin main
            self._mock_success(),  # checkout feature/test (finally)
        ]

        result = manager.merge_branch("feature/test", "main", push=True)

        assert result.success is True
        assert "Successfully merged" in result.message
        assert "feature/test" in result.message
        assert "main" in result.message

    @patch("subprocess.run")
    def test_merge_success_no_push(self, mock_run, manager) -> None:
        """Merge succeeds without push."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            self._mock_success(),  # fetch origin
            self._mock_success(),  # checkout main
            self._mock_success(),  # pull origin main
            subprocess.CompletedProcess(
                args=["git", "merge"], returncode=0, stdout="Merge made\n", stderr=""
            ),  # merge --no-ff
            # No push call
            self._mock_success(),  # checkout feature/test (finally)
        ]

        result = manager.merge_branch("feature/test", "main", push=False)

        assert result.success is True
        assert "Successfully merged" in result.message

    @patch("subprocess.run")
    def test_merge_fetch_failure(self, mock_run, manager) -> None:
        """Merge fails when fetch fails."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            subprocess.CompletedProcess(
                args=["git", "fetch"],
                returncode=128,
                stdout="",
                stderr="fatal: could not reach remote",
            ),  # fetch fails
        ]

        result = manager.merge_branch("feature/test", "main")

        assert result.success is False
        assert "Failed to fetch" in result.message
        assert "could not reach remote" in result.error

    @patch("subprocess.run")
    def test_merge_checkout_failure(self, mock_run, manager) -> None:
        """Merge fails when checkout fails."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            self._mock_success(),  # fetch origin
            subprocess.CompletedProcess(
                args=["git", "checkout"],
                returncode=1,
                stdout="",
                stderr="error: pathspec 'main' did not match",
            ),  # checkout fails
        ]

        result = manager.merge_branch("feature/test", "main")

        assert result.success is False
        assert "Failed to checkout" in result.message

    @patch("subprocess.run")
    def test_merge_pull_failure(self, mock_run, manager) -> None:
        """Merge fails when pull fails."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            self._mock_success(),  # fetch origin
            self._mock_success(),  # checkout main
            subprocess.CompletedProcess(
                args=["git", "pull"],
                returncode=1,
                stdout="",
                stderr="error: cannot pull with rebase",
            ),  # pull fails
            self._mock_success(),  # checkout feature/test (finally)
        ]

        result = manager.merge_branch("feature/test", "main")

        assert result.success is False
        assert "Failed to pull" in result.message

    @patch("subprocess.run")
    def test_merge_conflict(self, mock_run, manager) -> None:
        """Merge detects conflicts and aborts."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            self._mock_success(),  # fetch origin
            self._mock_success(),  # checkout main
            self._mock_success(),  # pull origin main
            subprocess.CompletedProcess(
                args=["git", "merge"],
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in src/foo.py",
                stderr="",
            ),  # merge fails with conflict
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="src/foo.py\nsrc/bar.py\n",
                stderr="",
            ),  # diff --name-only --diff-filter=U
            self._mock_success(),  # merge --abort
            self._mock_success(),  # checkout feature/test (finally)
        ]

        result = manager.merge_branch("feature/test", "main")

        assert result.success is False
        assert result.error == "merge_conflict"
        assert "2 files" in result.message
        assert "src/foo.py" in result.output
        assert "src/bar.py" in result.output

    @patch("subprocess.run")
    def test_merge_non_conflict_failure(self, mock_run, manager) -> None:
        """Merge fails with non-conflict error."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            self._mock_success(),  # fetch origin
            self._mock_success(),  # checkout main
            self._mock_success(),  # pull origin main
            subprocess.CompletedProcess(
                args=["git", "merge"],
                returncode=1,
                stdout="",
                stderr="fatal: refusing to merge unrelated histories",
            ),  # merge fails without conflict
            self._mock_success(),  # merge --abort
            self._mock_success(),  # checkout feature/test (finally)
        ]

        result = manager.merge_branch("feature/test", "main")

        assert result.success is False
        assert "Merge failed" in result.message
        assert "unrelated histories" in result.error

    @patch("subprocess.run")
    def test_merge_push_failure(self, mock_run, manager) -> None:
        """Merge succeeds but push fails (still reports success=True)."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            self._mock_success(),  # fetch origin
            self._mock_success(),  # checkout main
            self._mock_success(),  # pull origin main
            subprocess.CompletedProcess(
                args=["git", "merge"], returncode=0, stdout="Merge made\n", stderr=""
            ),  # merge succeeds
            subprocess.CompletedProcess(
                args=["git", "push"],
                returncode=1,
                stdout="",
                stderr="error: push rejected",
            ),  # push fails
            self._mock_success(),  # checkout feature/test (finally)
        ]

        result = manager.merge_branch("feature/test", "main", push=True)

        assert result.success is True
        assert "push failed" in result.message
        assert result.error == "error: push rejected"

    @patch("subprocess.run")
    def test_merge_timeout(self, mock_run, manager) -> None:
        """Merge handles timeout."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            self._mock_success(),  # fetch origin
            self._mock_success(),  # checkout main
            self._mock_success(),  # pull origin main
            subprocess.TimeoutExpired(cmd="git merge", timeout=60),  # merge times out
            self._mock_success(),  # merge --abort (in except)
            self._mock_success(),  # checkout feature/test (finally)
        ]

        result = manager.merge_branch("feature/test", "main")

        assert result.success is False
        assert "timed out" in result.message
        assert result.error == "timeout"

    @patch("subprocess.run")
    def test_merge_generic_exception(self, mock_run, manager) -> None:
        """Merge handles generic exception."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            self._mock_success(),  # fetch origin
            self._mock_success(),  # checkout main
            self._mock_success(),  # pull origin main
            Exception("Unexpected git crash"),  # merge raises
            self._mock_success(),  # merge --abort (in except)
            self._mock_success(),  # checkout feature/test (finally)
        ]

        result = manager.merge_branch("feature/test", "main")

        assert result.success is False
        assert "Merge error" in result.message
        assert result.error == "Unexpected git crash"

    @patch("subprocess.run")
    def test_merge_restores_original_branch(self, mock_run, manager) -> None:
        """Merge restores original branch in finally block."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/other"),  # on a different branch
            self._mock_success(),  # fetch origin
            self._mock_success(),  # checkout main
            self._mock_success(),  # pull origin main
            subprocess.CompletedProcess(
                args=["git", "merge"], returncode=0, stdout="Merge made\n", stderr=""
            ),  # merge succeeds
            self._mock_success(),  # push origin main
            self._mock_success(),  # checkout feature/other (finally)
        ]

        result = manager.merge_branch("feature/test", "main", push=True)

        assert result.success is True
        # Verify the last call was checkout back to feature/other
        last_call = mock_run.call_args_list[-1]
        assert "checkout" in last_call[0][0]
        assert "feature/other" in last_call[0][0]

    @patch("subprocess.run")
    def test_merge_conflict_in_stderr(self, mock_run, manager) -> None:
        """Merge detects conflict from stderr."""
        mock_run.side_effect = [
            self._mock_rev_parse("feature/test"),  # rev-parse HEAD
            self._mock_success(),  # fetch origin
            self._mock_success(),  # checkout main
            self._mock_success(),  # pull origin main
            subprocess.CompletedProcess(
                args=["git", "merge"],
                returncode=1,
                stdout="",
                stderr="CONFLICT (modify/delete): src/foo.py",
            ),  # merge fails with conflict in stderr
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="src/foo.py\n",
                stderr="",
            ),  # diff --name-only
            self._mock_success(),  # merge --abort
            self._mock_success(),  # checkout feature/test (finally)
        ]

        result = manager.merge_branch("feature/test", "main")

        assert result.success is False
        assert result.error == "merge_conflict"
        assert "1 files" in result.message
