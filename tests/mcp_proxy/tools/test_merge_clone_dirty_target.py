"""Real-repository coverage for merge_clone dirty-target landing."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.clones.git import CloneGitManager
from gobby.mcp_proxy.tools.clones import create_clones_registry
from gobby.mcp_proxy.tools.worktrees._merge_fallback import FastForwardLandingResult
from gobby.storage.clones import Clone
from tests.mcp_proxy.tools.test_merge_landscape import _commit_file, _init_git_repo

pytestmark = pytest.mark.integration


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _index_snapshot(repo: Path, path: str) -> tuple[str, str, bytes]:
    """Capture staged entry metadata, staged bytes, and worktree bytes for one path."""
    return (
        _git(repo, "ls-files", "--stage", "--", path),
        _git(repo, "diff", "--cached", "--binary", "--", path),
        (repo / path).read_bytes(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "target-sha", "fast-forward"])
async def test_merge_clone_fast_forwards_with_disjoint_staged_target_dirt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    repo = tmp_path / "repo"
    clone_path = tmp_path / "clone"
    repo.mkdir()
    _init_git_repo(repo)
    _commit_file(repo, "base.txt", "base\n")
    (repo / ".gobby").mkdir()
    _commit_file(repo, ".gobby/project.json", "{}\n")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(repo))
    _git(tmp_path, "clone", str(repo), str(clone_path))
    _git(clone_path, "checkout", "-b", "feature/path")
    _commit_file(clone_path, "feature.txt", "feature\n")
    source_head = _git(clone_path, "rev-parse", "refs/heads/feature/path")
    target_head = _git(repo, "rev-parse", "refs/heads/main")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    gobby_path = ".gobby/project.json"
    (repo / gobby_path).write_bytes(b'{"local":"staged"}\n')
    _git(repo, "add", gobby_path)
    (repo / gobby_path).write_bytes(b'{"local": true}\n')
    gobby_before = _index_snapshot(repo, gobby_path)

    now = datetime.now(UTC)
    clone = Clone(
        id="clone-real",
        project_id="11111111-1111-4111-8111-111111110001",
        branch_name="feature/path",
        clone_path=str(clone_path),
        base_branch="main",
        task_id=None,
        agent_session_id=None,
        status="active",
        remote_url=str(repo),
        last_sync_at=None,
        cleanup_after=None,
        created_at=now,
        updated_at=now,
    )
    clone_storage = MagicMock()
    clone_storage.get.return_value = clone
    git_manager = CloneGitManager(repo)
    run_git = git_manager.run_git_command
    stash_push_called = False

    async def run_with_fault(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal stash_push_called
        if args[:2] == ["stash", "push"]:
            stash_push_called = True
        if fault == "target-sha" and args == ["rev-parse", "refs/heads/main"]:
            raise subprocess.TimeoutExpired(["git", *args], 10)
        return await run_git(args, **kwargs)

    monkeypatch.setattr(git_manager, "run_git_command", run_with_fault)
    if fault == "fast-forward":

        async def fail_fast_forward(*args: Any, **kwargs: Any) -> FastForwardLandingResult:
            return FastForwardLandingResult(
                success=False,
                step="fast-forward",
                error="forced fast-forward failure",
            )

        monkeypatch.setattr(
            "gobby.mcp_proxy.tools._clones_operations.land_by_fast_forward",
            fail_fast_forward,
        )
    registry = create_clones_registry(
        clone_storage=clone_storage,
        git_manager=git_manager,
        project_id=clone.project_id,
    )

    result = await registry.call(
        "merge_clone",
        {"clone_id": clone.id, "target_branch": "main"},
    )

    assert _git(repo, "diff", "--cached", "--name-only") == ".gobby/project.json\nstaged.txt"
    assert (repo / "staged.txt").read_text(encoding="utf-8") == "staged\n"
    if fault == "fast-forward":
        assert result["success"] is False
        assert result["step"] == "fast-forward"
        assert _git(repo, "rev-parse", "refs/heads/main") == target_head
        clone_storage.mark_merged.assert_not_called()
        assert stash_push_called is False
    elif fault == "target-sha":
        assert result["success"] is False
        assert result["landing_state"] == "unknown"
        assert result["step"] == "verify-target"
        assert _git(repo, "rev-parse", "refs/heads/main") == source_head
        clone_storage.mark_merged.assert_not_called()
    else:
        assert result["success"] is True
        assert result["landing"] == "fast-forward"
        assert result["merge_sha"] == source_head
        assert _git(repo, "rev-parse", "refs/heads/main") == source_head
        clone_storage.mark_merged.assert_called_once()
    assert (repo / ".gobby/project.json").read_text() == '{"local": true}\n'
    assert _index_snapshot(repo, gobby_path) == gobby_before
