"""Tests for gobby.agents.checkpoint_manager module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.checkpoint_manager import CheckpointManager
from gobby.storage.checkpoints import Checkpoint, LocalCheckpointManager

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    return result.stdout


@pytest.fixture
def mock_storage() -> MagicMock:
    storage = MagicMock(spec=LocalCheckpointManager)
    storage.count_for_task.return_value = 0
    storage.create.side_effect = lambda c: c
    return storage


@pytest.fixture
def manager(mock_storage: MagicMock) -> CheckpointManager:
    return CheckpointManager(mock_storage)


class TestCreateCheckpoint:
    """Tests for CheckpointManager.create_checkpoint()."""

    def test_returns_none_when_no_changes(
        self, manager: CheckpointManager, mock_storage: MagicMock
    ) -> None:
        with patch.object(manager, "_run_git") as mock_git:
            mock_git.return_value = ""  # Empty status = no changes
            result = manager.create_checkpoint("/tmp/repo", "task-1", "sess-1", "run-1")
            assert result is None
            mock_storage.create.assert_not_called()

    def test_returns_none_when_status_is_none(
        self, manager: CheckpointManager, mock_storage: MagicMock
    ) -> None:
        with patch.object(manager, "_run_git") as mock_git:
            mock_git.return_value = None  # git failed
            result = manager.create_checkpoint("/tmp/repo", "task-1", "sess-1", "run-1")
            assert result is None

    def test_creates_checkpoint_on_dirty_tree(
        self, manager: CheckpointManager, mock_storage: MagicMock
    ) -> None:
        git_calls: list[list[str]] = []

        def mock_run_git(args: list[str], cwd: str, timeout: int = 30) -> str | None:
            git_calls.append(args)
            cmd = args[0]
            if cmd == "status":
                return " M file1.py\n M file2.py\n"
            elif cmd == "write-tree" and git_calls.count(["write-tree"]) == 1:
                return "original-index-tree\n"
            elif cmd == "add":
                return ""
            elif cmd == "write-tree":
                return "tree-sha-123\n"
            elif cmd == "rev-parse":
                return "parent-sha-456\n"
            elif cmd == "commit-tree":
                return "commit-sha-789\n"
            elif cmd == "update-ref":
                return ""
            elif cmd == "read-tree":
                return ""
            return None

        with patch.object(manager, "_run_git", side_effect=mock_run_git):
            result = manager.create_checkpoint("/tmp/repo", "task-1", "sess-1", "run-1")

        assert result is not None
        assert result.task_id == "task-1"
        assert result.commit_sha == "commit-sha-789"
        assert result.parent_sha == "parent-sha-456"
        assert result.files_changed == 2
        assert result.ref_name == "refs/gobby/ckpt/task-1/1"

        # Verify git command sequence
        assert git_calls[0] == ["status", "--porcelain"]
        assert git_calls[1] == ["write-tree"]
        assert git_calls[2] == ["add", "-u"]
        assert git_calls[3] == ["write-tree"]
        assert git_calls[4] == ["rev-parse", "HEAD"]
        assert git_calls[5][0] == "commit-tree"
        assert git_calls[6][0] == "update-ref"
        assert git_calls[7] == ["read-tree", "original-index-tree"]

        # Verify DB storage
        mock_storage.create.assert_called_once()
        stored = mock_storage.create.call_args[0][0]
        assert isinstance(stored, Checkpoint)
        assert stored.run_id == "run-1"

    def test_always_unstages_on_failure(
        self, manager: CheckpointManager, mock_storage: MagicMock
    ) -> None:
        """Ensures the original index is restored even when write-tree fails."""
        call_log: list[str] = []

        def mock_run_git(args: list[str], cwd: str, timeout: int = 30) -> str | None:
            call_log.append(args[0])
            if args[0] == "status":
                return " M file.py\n"
            elif args[0] == "write-tree" and call_log.count("write-tree") == 1:
                return "original-index-tree\n"
            elif args[0] == "add":
                return ""
            elif args[0] == "write-tree":
                return None  # Simulate failure
            elif args[0] == "read-tree":
                return ""
            return None

        with patch.object(manager, "_run_git", side_effect=mock_run_git):
            result = manager.create_checkpoint("/tmp/repo", "task-1", "sess-1", "run-1")

        assert result is None
        assert "read-tree" in call_log  # Original index is still restored in finally

    def test_restores_divergent_pre_existing_staged_blob(
        self,
        manager: CheckpointManager,
        tmp_path: Path,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        file_path = repo / "file.txt"

        _git(repo, "init")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test User")
        file_path.write_text("base\n")
        _git(repo, "add", "file.txt")
        _git(repo, "commit", "-m", "initial")

        file_path.write_text("staged\n")
        _git(repo, "add", "file.txt")
        staged_blob_before = _git(repo, "rev-parse", ":file.txt").strip()
        file_path.write_text("worktree\n")

        result = manager.create_checkpoint(repo, "task-1", "sess-1", "run-1")

        assert result is not None
        assert _git(repo, "rev-parse", ":file.txt").strip() == staged_blob_before
        assert _git(repo, "show", ":file.txt") == "staged\n"
        assert file_path.read_text() == "worktree\n"
        assert _git(repo, "show", f"{result.commit_sha}:file.txt") == "worktree\n"

    def test_increments_seq_from_storage_count(
        self, manager: CheckpointManager, mock_storage: MagicMock
    ) -> None:
        mock_storage.count_for_task.return_value = 3
        index_snapshotted = False

        def mock_run_git(args: list[str], cwd: str, timeout: int = 30) -> str | None:
            nonlocal index_snapshotted
            if args[0] == "status":
                return " M file.py\n"
            elif args[0] == "write-tree" and not index_snapshotted:
                index_snapshotted = True
                return "original-index-tree\n"
            elif args[0] == "add":
                return ""
            elif args[0] == "write-tree":
                return "tree\n"
            elif args[0] == "rev-parse":
                return "parent\n"
            elif args[0] == "commit-tree":
                return "commit\n"
            elif args[0] == "update-ref":
                return ""
            elif args[0] == "read-tree":
                return ""
            return None

        with patch.object(manager, "_run_git", side_effect=mock_run_git):
            result = manager.create_checkpoint("/tmp/repo", "task-1", "sess-1", "run-1")

        assert result is not None
        assert result.ref_name == "refs/gobby/ckpt/task-1/4"  # count=3, so seq=4


class TestRunGit:
    """Tests for CheckpointManager._run_git()."""

    def test_returns_stdout_on_success(self, manager: CheckpointManager) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="output\n")
            result = manager._run_git(["status"], "/tmp")
            assert result == "output\n"

    def test_returns_none_on_failure(self, manager: CheckpointManager) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error", stdout="")
            result = manager._run_git(["bad-cmd"], "/tmp")
            assert result is None

    def test_returns_none_on_timeout(self, manager: CheckpointManager) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)):
            result = manager._run_git(["slow-cmd"], "/tmp")
            assert result is None

    def test_returns_none_on_os_error(self, manager: CheckpointManager) -> None:
        with patch("subprocess.run", side_effect=OSError("git not found")):
            result = manager._run_git(["status"], "/tmp")
            assert result is None
