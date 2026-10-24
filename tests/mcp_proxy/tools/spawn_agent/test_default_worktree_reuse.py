"""The default agent continues development and binds the reused task checkout."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, TaskArtifactManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.workflows.agent_models import AgentPromptBlocks
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.agent_definitions import make_agent_definition
from tests.mcp_proxy.tools.spawn_agent.test_associated_worktree_join import git

pytestmark = pytest.mark.usefixtures("stub_srt_verifier")


@pytest.mark.integration
@pytest.mark.parametrize("reuse", ["explicit", "branch"])
async def test_default_body_merges_and_binds_reused_worktree(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    reuse: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Default Test")
    git(source, "commit", "--allow-empty", "-m", "initial")
    child = tmp_path / "developer"
    git(source, "worktree", "add", "-b", "candidate", str(child))
    (child / "candidate.txt").write_text("candidate\n")
    git(child, "add", ".")
    git(child, "commit", "-m", "candidate")
    candidate = git(child, "rev-parse", "HEAD")
    (source / "base.txt").write_text("base\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base advances")
    base = git(source, "rev-parse", "HEAD")
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Continue development",
        validation_criteria="Reuse merges the base and binds artifacts.",
    )
    storage = LocalWorktreeManager(temp_db)
    existing = storage.create(
        project_id=sample_project["id"],
        branch_name="candidate",
        worktree_path=str(child),
        base_branch="main",
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
        lambda *_: {"id": sample_project["id"], "project_path": str(source)},
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
        lambda *_: None,
    )
    repair = AsyncMock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment", repair
    )
    monkeypatch.setattr("gobby.agents.isolation_worktree.repair_isolation_environment", repair)
    prepare = MagicMock(side_effect=RuntimeError("stopped after isolation"))
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn", prepare
    )
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "ok", 0)
    runner.run_storage.has_active_run_for_task.return_value = False

    result = await spawn_agent_impl(
        prompt="continue",
        runner=runner,
        agent_body=make_agent_definition(
            name="default",
            prompts=AgentPromptBlocks(agent="Continue development"),
            task_step_writes=[],
        ),
        task_id=task.id,
        task_manager=tasks,
        parent_session_id="parent",
        provider="codex",
        worktree_id=existing.id if reuse == "explicit" else None,
        checkout_mode="worktree",
        branch_name="candidate",
        worktree_storage=storage,
        git_manager=WorktreeGitManager(source),
        base_branch="main",
    )

    assert result["error"] == "stopped after isolation"
    prepare.assert_called_once()
    assert git(child, "rev-parse", "HEAD^1") == candidate
    assert git(child, "rev-parse", "HEAD^2") == base
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.worktree_id == existing.id
    assert artifacts.worktree_path == str(child)
    assert artifacts.base_commit_sha == base
