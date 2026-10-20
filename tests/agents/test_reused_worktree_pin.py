"""Reusing a seat selects a new pin; resuming preserves its existing pin."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents import isolation_repair
from gobby.agents.isolation_models import SpawnConfig
from gobby.mcp_proxy.tools.spawn_agent._worktree_reuse import (
    prepare_resumed_worktree,
    prepare_reused_worktree,
)
from gobby.utils.project_context import ensure_project_json_for_isolation, read_isolation_marker
from gobby.worktrees.git import WorktreeGitManager
from tests.agents.test_clone_fork_commit import git


@pytest.mark.integration
@pytest.mark.parametrize("resume", [False, True], ids=["reuse", "resume"])
async def test_reused_or_resumed_worktree_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    source = tmp_path / "source"
    (source / ".gobby").mkdir(parents=True)
    (source / ".gobby" / "project.json").write_text(json.dumps({"id": "parent-project"}))
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Pin Test")
    git(source, "add", ".")
    git(source, "commit", "-m", "base A")
    base_a = git(source, "rev-parse", "HEAD")
    child = tmp_path / "seat"
    git(source, "worktree", "add", "-b", "seat", str(child))
    for name in (
        "_copy_cli_hooks",
        "_patch_mcp_config_for_isolation",
        "apply_isolation_git_hygiene",
    ):
        monkeypatch.setattr(isolation_repair, name, AsyncMock())
    monkeypatch.setattr(
        isolation_repair,
        "preseed_isolated_python_environment",
        AsyncMock(return_value=SimpleNamespace(attempted=False)),
    )
    monkeypatch.setattr("gobby.utils.project_context.link_checkout_cargo_target", lambda *a: None)
    await ensure_project_json_for_isolation(source, child, base_commit=base_a)
    git(source, "commit", "--allow-empty", "-m", "base B")
    base_b = git(source, "rev-parse", "HEAD")
    existing = SimpleNamespace(
        id="worktree", worktree_path=str(child), branch_name="seat", base_branch="main"
    )
    config = SpawnConfig(
        prompt="pin",
        task_id=None,
        task_title=None,
        task_seq_num=None,
        branch_name="seat",
        branch_prefix=None,
        base_branch="main",
        project_id="parent-project",
        project_path=str(source),
        provider="codex",
        parent_session_id="parent",
    )

    if resume:
        await prepare_resumed_worktree(
            existing_worktree=existing, spawn_config=config, main_repo_path=str(source)
        )
    else:
        await prepare_reused_worktree(
            existing_worktree=existing,
            git_manager=WorktreeGitManager(source),
            worktree_storage=MagicMock(),
            spawn_config=config,
            main_repo_path=str(source),
        )

    expected = base_a if resume else base_b
    assert git(child, "rev-parse", "HEAD") == expected
    marker = read_isolation_marker(child)
    assert marker is not None
    assert marker["base_commit"] == expected
    assert marker["ancestor_checkout_path"] == str(source.resolve())
