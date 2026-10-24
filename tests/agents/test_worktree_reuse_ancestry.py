"""Reused developer worktrees retain exact task checkpoint commits."""

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.agents.isolation_models import SpawnConfig
from gobby.mcp_proxy.tools.spawn_agent._worktree_reuse import prepare_reused_worktree
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.worktrees.git import WorktreeGitManager


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.integration
@pytest.mark.parametrize("linked", [True, False], ids=["linked", "unlinked"])
async def test_reused_worktree_merges_base_without_rewriting_checkpoints(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    linked: bool,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Reuse Test")
    git(source, "commit", "--allow-empty", "-m", "initial")
    child = tmp_path / "developer"
    git(source, "worktree", "add", "-b", "candidate", str(child))
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Preserve checkpoints",
        validation_criteria="Every checkpoint remains reachable after reuse.",
    )
    checkpoints: list[str] = []
    for index in range(2):
        (child / f"checkpoint-{index}.txt").write_text(f"checkpoint {index}\n")
        git(child, "add", ".")
        git(child, "commit", "-m", f"checkpoint {index}")
        sha = git(child, "rev-parse", "HEAD")
        checkpoints.append(sha)
        if linked:
            tasks.link_commit(task.id, sha)
    (source / "base.txt").write_text("base advanced\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base advances")
    base = git(source, "rev-parse", "HEAD")
    storage = LocalWorktreeManager(temp_db)
    existing = storage.create(
        project_id=sample_project["id"],
        branch_name="candidate",
        worktree_path=str(child),
        base_branch="main",
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment",
        AsyncMock(),
    )
    config = SpawnConfig(
        prompt="continue",
        task_id=task.id,
        task_title=task.title,
        task_seq_num=task.seq_num,
        branch_name="candidate",
        branch_prefix=None,
        base_branch="main",
        project_id=sample_project["id"],
        project_path=str(source),
        provider="codex",
        parent_session_id="parent",
    )

    context, _ = await prepare_reused_worktree(
        existing_worktree=existing,
        git_manager=WorktreeGitManager(source),
        worktree_storage=storage,
        spawn_config=config,
        main_repo_path=str(source),
    )

    reachable = git(child, "rev-list", "HEAD").splitlines()
    assert all(sha in reachable for sha in checkpoints)
    assert base in reachable
    assert git(child, "rev-parse", "HEAD^1") == checkpoints[-1]
    assert git(child, "rev-parse", "HEAD^2") == base
    assert git(child, "status", "--porcelain") == ""
    assert context.extra["base_commit_sha"] == base
    if linked:
        assert tasks.get_task(task.id).commits == checkpoints
