"""Tests for atomic terminal-agent worktree checkpoints."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from gobby.agents import worktree_checkpoint as checkpoint_module
from gobby.agents.worktree_checkpoint import (
    CHECKPOINT_AUTHOR_EMAIL,
    CHECKPOINT_AUTHOR_NAME,
    WorktreeCheckpointError,
    checkpoint_commit_message,
    checkpoint_worktree,
)
from gobby.agents.worktree_reuse import sync_reused_worktree_to_base
from gobby.worktrees.git import WorktreeGitManager


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_checkpoint_commits_tracked_staged_and_untracked_content(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("working\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")

    checkpoint = checkpoint_worktree(
        worktree_path=repo,
        expected_paths={"tracked.txt", "untracked.txt"},
        task_seq_num=21897,
        run_id="run-123",
    )

    assert checkpoint.included_paths == ("tracked.txt", "untracked.txt")
    assert checkpoint.commit_sha == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "show", "HEAD:tracked.txt") == "working"
    assert _git(repo, "show", "HEAD:untracked.txt") == "new"
    assert _git(repo, "show", "-s", "--format=%an", "HEAD") == CHECKPOINT_AUTHOR_NAME
    assert _git(repo, "show", "-s", "--format=%ae", "HEAD") == CHECKPOINT_AUTHOR_EMAIL
    assert _git(repo, "show", "-s", "--format=%B", "HEAD") == checkpoint_commit_message(
        task_seq_num=21897,
        run_id="run-123",
    )


def test_checkpoint_refuses_changed_dirty_set_without_mutation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    old_head = _git(repo, "rev-parse", "HEAD")
    old_status = _git(repo, "status", "--porcelain=v1")

    with pytest.raises(WorktreeCheckpointError, match="dirty set changed") as exc_info:
        checkpoint_worktree(
            worktree_path=repo,
            expected_paths={"different.txt"},
            task_seq_num=21897,
            run_id="run-123",
        )

    assert exc_info.value.error_code == "dirty_set_changed"
    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert _git(repo, "status", "--porcelain=v1") == old_status


def test_commit_failure_preserves_head_index_and_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("working\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    old_head = _git(repo, "rev-parse", "HEAD")
    old_status = _git(repo, "status", "--porcelain=v1")
    index_path = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    old_index = index_path.read_bytes()
    real_run_git = checkpoint_module._run_git

    def fail_commit_tree(
        path: Path,
        arguments: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if arguments[0] == "commit-tree":
            return subprocess.CompletedProcess(
                args=["git", *arguments],
                returncode=1,
                stdout="",
                stderr="forced commit failure",
            )
        return real_run_git(path, arguments, env=env, input_text=input_text)

    monkeypatch.setattr(checkpoint_module, "_run_git", fail_commit_tree)

    with pytest.raises(WorktreeCheckpointError, match="forced commit failure"):
        checkpoint_worktree(
            worktree_path=repo,
            expected_paths={"tracked.txt", "untracked.txt"},
            task_seq_num=21897,
            run_id="run-123",
        )

    assert _git(repo, "rev-parse", "HEAD") == old_head
    assert index_path.read_bytes() == old_index
    assert _git(repo, "status", "--porcelain=v1") == old_status


@pytest.mark.asyncio
async def test_checkpointed_linked_worktree_is_clean_for_respawn_reuse(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base_branch = _git(repo, "branch", "--show-current")
    worktree = tmp_path / "task-worktree"
    _git(repo, "worktree", "add", "-b", "task-42", str(worktree), base_branch)
    (worktree / "tracked.txt").write_text("recovered\n", encoding="utf-8")
    (worktree / "untracked.txt").write_text("new\n", encoding="utf-8")

    checkpoint = checkpoint_worktree(
        worktree_path=str(worktree),
        expected_paths={"tracked.txt", "untracked.txt"},
        task_seq_num=42,
        run_id="terminal-run",
    )
    sync_result = await sync_reused_worktree_to_base(
        git_manager=WorktreeGitManager(repo),
        worktree_path=str(worktree),
        base_branch=base_branch,
    )

    assert _git(worktree, "status", "--porcelain") == ""
    assert _git(worktree, "rev-parse", "HEAD") == checkpoint.commit_sha
    assert sync_result.base_commit_sha == _git(repo, "rev-parse", base_branch)
