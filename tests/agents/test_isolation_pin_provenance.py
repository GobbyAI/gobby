"""Agent launch markers pin the selected base even when source HEAD differs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents import isolation_repair
from gobby.agents.isolation_clone import CloneIsolationHandler
from gobby.agents.isolation_models import IsolationHandler, SpawnConfig
from gobby.agents.isolation_worktree import WorktreeIsolationHandler
from gobby.clones.git import CloneGitManager
from gobby.utils.project_context import read_isolation_marker
from gobby.worktrees.git import WorktreeGitManager
from tests.agents.test_clone_fork_commit import git


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["clone", "worktree"])
async def test_agent_marker_records_selected_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    source = tmp_path / "source"
    (source / ".gobby").mkdir(parents=True)
    (source / ".gobby" / "project.json").write_text(json.dumps({"id": "parent-project"}))
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Pin Test")
    git(source, "add", ".")
    git(source, "commit", "-m", "selected base")
    selected = git(source, "rev-parse", "HEAD")
    git(source, "switch", "-c", "caller")
    git(source, "commit", "--allow-empty", "-m", "caller advances")
    child = tmp_path / "clones" / "agent" if kind == "clone" else tmp_path / "agent"
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
    storage = MagicMock()
    storage.get_by_branch.return_value = None
    storage.create.side_effect = lambda **values: SimpleNamespace(id="isolated", **values)
    git_manager = WorktreeGitManager(source)
    handler: IsolationHandler
    if kind == "clone":
        monkeypatch.setattr("gobby.clones.git.CLONES_ROOT", child.parent)
        handler = CloneIsolationHandler(CloneGitManager(source), storage, git_manager)
        monkeypatch.setattr(handler, "_generate_clone_path", lambda *a: str(child))
    else:
        handler = WorktreeIsolationHandler(git_manager, storage)
        monkeypatch.setattr(handler, "_generate_worktree_path", lambda *a: str(child))
    config = SpawnConfig(
        prompt="pin",
        task_id=None,
        task_title=None,
        task_seq_num=None,
        branch_name="agent",
        branch_prefix=None,
        base_branch="main",
        project_id="parent-project",
        project_path=str(source),
        provider="codex",
        parent_session_id="parent",
    )

    await handler.prepare_environment(config)

    assert git(child, "rev-parse", "HEAD") == selected
    marker = read_isolation_marker(child)
    assert marker is not None
    assert marker["base_commit"] == selected
    assert marker["ancestor_checkout_path"] == str(source.resolve())
