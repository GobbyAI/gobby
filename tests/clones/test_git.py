"""Tests for gobby.clones.git module.

Tests for CloneGitManager with shallow_clone, sync_clone, delete_clone methods.
Uses mock subprocess calls to test git command execution.
"""

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.clones.git import CloneGitManager

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _managed_clones_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Let creation-path safety checks accept temp clone paths in unit tests."""
    monkeypatch.setattr("gobby.clones.git.CLONES_ROOT", tmp_path)


class TestCloneGitManagerInit:
    """Tests for CloneGitManager initialization."""

    def test_init_stores_repo_path(self, tmp_path: Path) -> None:
        """Manager stores repository path."""
        from gobby.clones.git import CloneGitManager

        manager = CloneGitManager(repo_path=tmp_path)

        assert manager.repo_path == tmp_path

    def test_init_accepts_string_path(self, tmp_path: Path) -> None:
        """Manager accepts string path."""
        from gobby.clones.git import CloneGitManager

        manager = CloneGitManager(repo_path=str(tmp_path))

        assert manager.repo_path == tmp_path

    def test_init_raises_for_nonexistent_path(self) -> None:
        """Manager raises ValueError for nonexistent path."""
        from gobby.clones.git import CloneGitManager

        with pytest.raises(ValueError, match="does not exist"):
            CloneGitManager(repo_path="/nonexistent/path")


class TestCloneGitManagerShallowClone:
    """Tests for CloneGitManager.shallow_clone method."""

    @pytest.fixture
    def manager(self, tmp_path: Path):
        """Create manager with temp directory as repo path."""
        from gobby.clones.git import CloneGitManager

        return CloneGitManager(repo_path=tmp_path)

    @pytest.fixture
    def mock_run(self):
        """Mock subprocess.run."""
        with patch("subprocess.run") as mock:
            yield mock

    def test_shallow_clone_success(self, manager, mock_run, tmp_path: Path) -> None:
        """Shallow clone creates clone with depth 1."""
        mock_run.return_value = MagicMock(returncode=0, stdout="Cloning into 'clone'...", stderr="")
        clone_path = tmp_path / "test_clone"

        result = manager.shallow_clone(
            remote_url="https://github.com/user/repo.git",
            clone_path=clone_path,
            branch="main",
        )

        assert result.success is True
        assert "created" in result.message.lower() or "cloned" in result.message.lower()
        # Verify git clone was called with --depth 1 (first call, before config)
        clone_call = mock_run.call_args_list[0]
        cmd = clone_call[0][0]
        assert "clone" in cmd
        assert "--depth" in cmd
        assert "1" in cmd

    def test_shallow_clone_redacts_query_and_preserves_ipv6(
        self,
        manager: CloneGitManager,
        mock_run: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        caplog.set_level(logging.DEBUG, logger="gobby.clones.git")

        manager.shallow_clone(
            remote_url=("https://user:password@[2001:db8::1]:8443/repo.git?token=secret#fragment"),
            clone_path=tmp_path / "test_clone",
            branch="main",
        )

        assert "https://[2001:db8::1]:8443/repo.git" in caplog.text
        assert "password" not in caplog.text
        assert "token=secret" not in caplog.text
        assert "fragment" not in caplog.text

    def test_shallow_clone_sets_pull_rebase(self, manager, mock_run, tmp_path: Path) -> None:
        """Shallow clone sets pull.rebase=true after successful clone."""
        mock_run.return_value = MagicMock(returncode=0, stdout="Cloning into...", stderr="")
        clone_path = tmp_path / "test_clone"

        result = manager.shallow_clone(
            remote_url="https://github.com/user/repo.git",
            clone_path=clone_path,
            branch="main",
        )

        assert result.success is True
        # Second call should be git config pull.rebase true
        assert mock_run.call_count >= 2
        config_call = mock_run.call_args_list[1]
        config_cmd = config_call[0][0]
        assert "config" in config_cmd
        assert "pull.rebase" in config_cmd
        assert "true" in config_cmd

    def test_shallow_clone_with_custom_depth(self, manager, mock_run, tmp_path: Path) -> None:
        """Shallow clone respects custom depth parameter."""
        mock_run.return_value = MagicMock(returncode=0, stdout="Cloning into 'clone'...", stderr="")
        clone_path = tmp_path / "test_clone"

        manager.shallow_clone(
            remote_url="https://github.com/user/repo.git",
            clone_path=clone_path,
            branch="main",
            depth=10,
        )

        clone_call = mock_run.call_args_list[0]
        cmd = clone_call[0][0]
        assert "--depth" in cmd
        depth_idx = cmd.index("--depth")
        assert cmd[depth_idx + 1] == "10"

    def test_shallow_clone_specifies_branch(self, manager, mock_run, tmp_path: Path) -> None:
        """Shallow clone uses specified branch."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        clone_path = tmp_path / "test_clone"

        manager.shallow_clone(
            remote_url="https://github.com/user/repo.git",
            clone_path=clone_path,
            branch="develop",
        )

        clone_call = mock_run.call_args_list[0]
        cmd = clone_call[0][0]
        assert "-b" in cmd or "--branch" in cmd
        # Find branch arg
        if "-b" in cmd:
            branch_idx = cmd.index("-b")
        else:
            branch_idx = cmd.index("--branch")
        assert cmd[branch_idx + 1] == "develop"

    def test_shallow_clone_fails_when_path_exists(self, manager, mock_run, tmp_path: Path) -> None:
        """Shallow clone fails if target path already exists."""
        clone_path = tmp_path / "existing"
        clone_path.mkdir()

        result = manager.shallow_clone(
            remote_url="https://github.com/user/repo.git",
            clone_path=clone_path,
            branch="main",
        )

        assert result.success is False
        assert "exists" in result.message.lower()

    def test_shallow_clone_handles_git_error(self, manager, mock_run, tmp_path: Path) -> None:
        """Shallow clone handles git command failure."""
        clone_path = tmp_path / "test_clone"

        run_count = 0

        def run_clone_with_partial_dir(*_args: object, **_kwargs: object) -> MagicMock:
            nonlocal run_count
            run_count += 1
            if run_count == 1:
                clone_path.mkdir()
                (clone_path / ".git").mkdir()
                return MagicMock(returncode=128, stdout="", stderr="fatal: repository not found")
            return MagicMock(returncode=0, stdout="Cloning into 'test_clone'...", stderr="")

        mock_run.side_effect = run_clone_with_partial_dir

        result = manager.shallow_clone(
            remote_url="https://github.com/user/nonexistent.git",
            clone_path=clone_path,
            branch="main",
        )

        assert result.success is False
        assert "repository" in result.message.lower() or "failed" in result.message.lower()
        assert not clone_path.exists()

        retry_result = manager.shallow_clone(
            remote_url="https://github.com/user/repo.git",
            clone_path=clone_path,
            branch="main",
        )

        assert retry_result.success is True

    def test_shallow_clone_handles_timeout(self, manager, mock_run, tmp_path: Path) -> None:
        """Shallow clone handles command timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["git"], timeout=30)
        clone_path = tmp_path / "test_clone"

        result = manager.shallow_clone(
            remote_url="https://github.com/user/repo.git",
            clone_path=clone_path,
            branch="main",
        )

        assert result.success is False
        assert "timed out" in result.message.lower()

    def test_shallow_clone_uses_single_branch(self, manager, mock_run, tmp_path: Path) -> None:
        """Shallow clone uses --single-branch for efficiency."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        clone_path = tmp_path / "test_clone"

        manager.shallow_clone(
            remote_url="https://github.com/user/repo.git",
            clone_path=clone_path,
            branch="main",
        )

        clone_call = mock_run.call_args_list[0]
        cmd = clone_call[0][0]
        assert "--single-branch" in cmd


class TestCloneGitManagerSyncClone:
    """Tests for CloneGitManager.sync_clone method."""

    @pytest.fixture
    def manager(self, tmp_path: Path):
        """Create manager with temp directory as repo path."""
        from gobby.clones.git import CloneGitManager

        return CloneGitManager(repo_path=tmp_path)

    @pytest.fixture
    def mock_run(self):
        """Mock subprocess.run."""
        with patch("subprocess.run") as mock:
            yield mock

    def test_sync_clone_pull_success(self, manager, mock_run, tmp_path: Path) -> None:
        """Sync clone pulls changes successfully."""
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="Already up to date.", stderr="")

        result = manager.sync_clone(clone_path, direction="pull")

        assert result.success is True
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "pull" in cmd

    def test_sync_clone_pull_uses_rebase(self, manager, mock_run, tmp_path: Path) -> None:
        """Sync clone pull uses --rebase to handle divergent branches."""
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="Already up to date.", stderr="")

        result = manager.sync_clone(clone_path, direction="pull")

        assert result.success is True
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "--rebase" in cmd

    def test_sync_clone_push_success(self, manager, mock_run, tmp_path: Path) -> None:
        """Sync clone pushes changes successfully."""
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="Everything up-to-date", stderr="")

        result = manager.sync_clone(clone_path, direction="push")

        assert result.success is True
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "push" in cmd

    def test_sync_clone_pull_push_success(self, manager, mock_run, tmp_path: Path) -> None:
        """Sync clone does pull then push for 'both' direction."""
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = manager.sync_clone(clone_path, direction="both")

        assert result.success is True
        # Should have called git at least twice (pull and push)
        assert mock_run.call_count >= 2

    def test_sync_clone_nonexistent_path_fails(self, manager, mock_run, tmp_path: Path) -> None:
        """Sync clone fails if path doesn't exist."""
        clone_path = tmp_path / "nonexistent"

        result = manager.sync_clone(clone_path, direction="pull")

        assert result.success is False
        assert "not exist" in result.message.lower() or "does not exist" in result.message.lower()

    def test_sync_clone_handles_conflict(self, manager, mock_run, tmp_path: Path) -> None:
        """Sync clone reports conflicts on pull failure."""
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="CONFLICT (content): Merge conflict in file.txt",
            stderr="",
        )

        result = manager.sync_clone(clone_path, direction="pull")

        assert result.success is False
        # Should indicate conflict
        assert "conflict" in result.message.lower() or "failed" in result.message.lower()

    def test_sync_clone_push_rejected(self, manager, mock_run, tmp_path: Path) -> None:
        """Sync clone handles push rejection."""
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="! [rejected] main -> main (non-fast-forward)",
            stderr="",
        )

        result = manager.sync_clone(clone_path, direction="push")

        assert result.success is False
        assert "rejected" in result.error.lower() or "failed" in result.message.lower()

    def test_sync_clone_handles_timeout(self, manager, mock_run, tmp_path: Path) -> None:
        """Sync clone handles command timeout."""
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["git"], timeout=60)

        result = manager.sync_clone(clone_path, direction="pull")

        assert result.success is False
        assert "timed out" in result.message.lower()


class TestCloneGitManagerDeleteClone:
    """Tests for CloneGitManager.delete_clone method."""

    @pytest.fixture
    def clones_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point clone deletion safety checks at the temp clones root."""
        clones_root = tmp_path / ".gobby" / "clones"
        monkeypatch.setattr("gobby.clones.git.CLONES_ROOT", clones_root)
        return clones_root

    @pytest.fixture
    def manager(self, tmp_path: Path, clones_root: Path):
        """Create manager with temp directory as repo path."""
        from gobby.clones.git import CloneGitManager

        return CloneGitManager(repo_path=tmp_path)

    def test_delete_clone_success(self, manager, clones_root: Path) -> None:
        """Delete clone removes directory successfully."""
        from gobby.clones.git import CloneStatus

        clone_path = clones_root / "project" / "clone"
        clone_path.mkdir(parents=True)
        (clone_path / "file.txt").write_text("content")

        status = CloneStatus(
            has_uncommitted_changes=False,
            has_staged_changes=False,
            has_untracked_files=False,
            branch="main",
            commit="abc1234",
        )
        with patch.object(manager, "get_clone_status", return_value=status):
            result = manager.delete_clone(clone_path)

        assert result.success is True
        assert not clone_path.exists()

    def test_delete_clone_nonexistent_path(self, manager, clones_root: Path) -> None:
        """Delete clone handles nonexistent path gracefully."""
        clone_path = clones_root / "project" / "nonexistent"

        result = manager.delete_clone(clone_path)

        # Should succeed (or report already gone) - idempotent
        assert result.success is True or "not exist" in result.message.lower()

    def test_delete_clone_with_nested_dirs(self, manager, clones_root: Path) -> None:
        """Delete clone removes nested directory structure."""
        from gobby.clones.git import CloneStatus

        clone_path = clones_root / "project" / "clone"
        (clone_path / "nested" / "deep").mkdir(parents=True)
        (clone_path / "nested" / "deep" / "file.txt").write_text("content")

        status = CloneStatus(
            has_uncommitted_changes=False,
            has_staged_changes=False,
            has_untracked_files=False,
            branch="main",
            commit="abc1234",
        )
        with patch.object(manager, "get_clone_status", return_value=status):
            result = manager.delete_clone(clone_path)

        assert result.success is True
        assert not clone_path.exists()

    def test_delete_clone_force_option(self, manager, clones_root: Path) -> None:
        """Delete clone respects force option."""
        clone_path = clones_root / "project" / "clone"
        clone_path.mkdir(parents=True)
        (clone_path / "file.txt").write_text("content")

        result = manager.delete_clone(clone_path, force=True)

        assert result.success is True
        assert not clone_path.exists()

    def test_delete_clone_plain_non_git_directory_fails_closed(
        self, manager, clones_root: Path
    ) -> None:
        """Do not delete a plain directory when force is false."""
        clone_path = clones_root / "project" / "plain-directory"
        clone_path.mkdir(parents=True)
        (clone_path / "file.txt").write_text("content")

        result = manager.delete_clone(clone_path, force=False)

        assert result.success is False
        assert result.error == "invalid_clone_path"
        assert clone_path.exists()
        assert (clone_path / "file.txt").exists()

    def test_delete_clone_rejects_status_without_branch_or_commit(
        self, manager, clones_root: Path
    ) -> None:
        """Do not delete when status lacks git identity metadata."""
        from gobby.clones.git import CloneStatus

        clone_path = clones_root / "project" / "unknown"
        clone_path.mkdir(parents=True)
        (clone_path / "file.txt").write_text("content")
        status = CloneStatus(
            has_uncommitted_changes=False,
            has_staged_changes=False,
            has_untracked_files=False,
            branch=None,
            commit=None,
        )

        with patch.object(manager, "get_clone_status", return_value=status):
            result = manager.delete_clone(clone_path, force=False)

        assert result.success is False
        assert result.error == "invalid_clone_path"
        assert clone_path.exists()
        assert (clone_path / "file.txt").exists()

    @pytest.mark.parametrize("force", [False, True])
    def test_delete_clone_rejects_paths_outside_clones_root(
        self, manager, tmp_path: Path, force: bool
    ) -> None:
        """Never delete paths outside the managed clones root."""
        clone_path = tmp_path / "outside"
        clone_path.mkdir()
        (clone_path / "file.txt").write_text("content")

        result = manager.delete_clone(clone_path, force=force)

        assert result.success is False
        assert "outside clones root" in result.message
        assert clone_path.exists()
        assert (clone_path / "file.txt").exists()


class TestCloneGitManagerGetRemoteUrl:
    """Tests for CloneGitManager.get_remote_url method."""

    @pytest.fixture
    def manager(self, tmp_path: Path):
        """Create manager with temp directory as repo path."""
        from gobby.clones.git import CloneGitManager

        return CloneGitManager(repo_path=tmp_path)

    @pytest.fixture
    def mock_run(self):
        """Mock subprocess.run."""
        with patch("subprocess.run") as mock:
            yield mock

    def test_get_remote_url_success(self, manager, mock_run) -> None:
        """Get remote URL returns origin URL."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/user/repo.git\n",
            stderr="",
        )

        result = manager.get_remote_url()

        assert result == "https://github.com/user/repo.git"

    def test_get_remote_url_ssh(self, manager, mock_run) -> None:
        """Get remote URL handles SSH URLs."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="git@github.com:user/repo.git\n",
            stderr="",
        )

        result = manager.get_remote_url()

        assert result == "git@github.com:user/repo.git"

    def test_get_remote_url_no_remote(self, manager, mock_run) -> None:
        """Get remote URL returns None if no remote."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="fatal: No such remote 'origin'",
        )

        result = manager.get_remote_url()

        assert result is None


class TestCloneGitManagerGetCloneStatus:
    """Tests for CloneGitManager.get_clone_status method."""

    @pytest.fixture
    def manager(self, tmp_path: Path):
        """Create manager with temp directory as repo path."""
        from gobby.clones.git import CloneGitManager

        return CloneGitManager(repo_path=tmp_path)

    @pytest.fixture
    def mock_run(self):
        """Mock subprocess.run."""
        with patch("subprocess.run") as mock:
            yield mock

    def test_get_clone_status_clean(self, manager, mock_run, tmp_path: Path) -> None:
        """Get clone status for clean working tree."""
        clone_path = tmp_path / "clone"
        clone_path.mkdir()

        # Mock branch, commit, and status calls
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="main\n", stderr=""),  # branch
            MagicMock(returncode=0, stdout="abc1234\n", stderr=""),  # commit
            MagicMock(returncode=0, stdout="", stderr=""),  # status (clean)
        ]

        status = manager.get_clone_status(clone_path)

        assert status is not None
        assert status.branch == "main"
        assert status.commit == "abc1234"
        assert status.has_uncommitted_changes is False

    def test_get_clone_status_with_changes(self, manager, mock_run, tmp_path: Path) -> None:
        """Get clone status with uncommitted changes."""
        clone_path = tmp_path / "clone"
        clone_path.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=0, stdout="abc1234\n", stderr=""),
            MagicMock(returncode=0, stdout=" M file.txt\n", stderr=""),  # Modified
        ]

        status = manager.get_clone_status(clone_path)

        assert status is not None
        assert status.has_uncommitted_changes is True

    def test_get_clone_status_nonexistent_path(self, manager, mock_run, tmp_path: Path) -> None:
        """Get clone status returns None for nonexistent path."""
        clone_path = tmp_path / "nonexistent"

        status = manager.get_clone_status(clone_path)

        assert status is None

    def test_get_clone_status_returns_none_when_git_status_fails(
        self, manager, mock_run, tmp_path: Path
    ) -> None:
        """Get clone status fails closed when git status cannot inspect the path."""
        clone_path = tmp_path / "clone"
        clone_path.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="not a git repository"),
            MagicMock(returncode=1, stdout="", stderr="not a git repository"),
            MagicMock(returncode=128, stdout="", stderr="not a git repository"),
        ]

        status = manager.get_clone_status(clone_path)

        assert status is None

    def test_get_clone_status_handles_os_error(self, manager, mock_run, tmp_path: Path) -> None:
        clone_path = tmp_path / "clone"
        clone_path.mkdir()
        mock_run.side_effect = OSError("git unavailable")

        assert manager.get_clone_status(clone_path) is None


class TestMergeBranch:
    """Tests for CloneGitManager.merge_branch."""

    @pytest.fixture
    def manager(self, tmp_path: Path):
        from gobby.clones.git import CloneGitManager

        return CloneGitManager(repo_path=tmp_path)

    @pytest.fixture
    def mock_run(self):
        with patch("gobby.clones.git.CloneGitManager._run_git") as mock:
            yield mock

    def test_merge_branch_uses_origin_prefix_by_default(
        self, manager, mock_run, tmp_path: Path
    ) -> None:
        """merge_branch prefixes source with origin/ by default."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        manager.merge_branch(source_branch="feature", target_branch="main")

        # Find the merge call (4th: rev-parse, fetch, checkout, pull, merge)
        merge_call = mock_run.call_args_list[4]
        assert merge_call[0][0] == ["merge", "origin/feature", "--no-edit"]

    def test_merge_branch_restores_detached_commit(self, manager, mock_run, tmp_path: Path) -> None:
        success = MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="HEAD\n", stderr=""),
            MagicMock(returncode=0, stdout="abc123def456\n", stderr=""),
            success,
            success,
            success,
            success,
            success,
        ]

        result = manager.merge_branch(source_branch="feature", target_branch="main")

        assert result.success is True
        assert mock_run.call_args_list[-1][0][0] == ["checkout", "abc123def456"]

    def test_merge_branch_rejects_unresolvable_detached_head(
        self, manager, mock_run, tmp_path: Path
    ) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="HEAD\n", stderr=""),
            MagicMock(returncode=128, stdout="", stderr="bad revision"),
        ]

        result = manager.merge_branch(source_branch="feature", target_branch="main")

        assert result.success is False
        assert result.error == "bad revision"
        assert mock_run.call_count == 2

    def test_merge_branch_source_is_local_skips_origin_prefix(
        self, manager, mock_run, tmp_path: Path
    ) -> None:
        """merge_branch with source_is_local=True uses branch name directly."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        manager.merge_branch(
            source_branch="clone-merge/0.2.28",
            target_branch="main",
            source_is_local=True,
        )

        # Find the merge call
        merge_call = mock_run.call_args_list[4]
        assert merge_call[0][0] == ["merge", "clone-merge/0.2.28", "--no-edit"]
