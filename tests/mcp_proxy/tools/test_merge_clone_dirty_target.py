"""Real-repository coverage for merge_clone dirty-target landing."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.clones.git import CloneGitManager
from gobby.mcp_proxy.tools.clones import create_clones_registry
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


@pytest.mark.asyncio
async def test_merge_clone_fast_forwards_with_disjoint_staged_target_dirt(
    tmp_path: Path,
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
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

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
    registry = create_clones_registry(
        clone_storage=clone_storage,
        git_manager=CloneGitManager(repo),
        project_id=clone.project_id,
    )

    result = await registry.call(
        "merge_clone",
        {"clone_id": clone.id, "target_branch": "main"},
    )

    assert result["success"] is True
    assert result["landing"] == "fast-forward"
    assert result["merge_sha"] == source_head
    assert _git(repo, "rev-parse", "refs/heads/main") == source_head
    assert _git(repo, "diff", "--cached", "--name-only") == "staged.txt"
    assert (repo / "staged.txt").read_text(encoding="utf-8") == "staged\n"
    clone_storage.mark_merged.assert_called_once()
