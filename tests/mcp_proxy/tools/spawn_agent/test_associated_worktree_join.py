"""Associated seats join a developer's exact candidate checkout."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.workflows.agent_models import AgentPromptBlocks
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.agent_definitions import make_agent_definition

pytestmark = pytest.mark.usefixtures("stub_srt_verifier")


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.integration
@pytest.mark.parametrize("reuse", ["explicit", "branch"])
@pytest.mark.parametrize("role", ["code-reviewer", "test-writer"])
@pytest.mark.parametrize("state", ["behind", "diverged", "conflict"])
async def test_associated_spawn_preserves_developer_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, state: str, reuse: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Join Test")
    (source / "shared.txt").write_text("initial\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    child = tmp_path / "developer"
    git(source, "worktree", "add", "-b", "candidate", str(child))
    if state != "behind":
        (child / ("shared.txt" if state == "conflict" else "candidate.txt")).write_text(
            "candidate\n"
        )
        git(child, "add", ".")
        git(child, "commit", "-m", "candidate")
    candidate = git(child, "rev-parse", "HEAD")
    (source / "shared.txt").write_text("base advances\n")
    git(source, "commit", "-am", "base advances")
    storage = MagicMock()
    storage.resolve_reference.return_value = "worktree"
    storage.get.return_value = SimpleNamespace(
        id="worktree", worktree_path=str(child), branch_name="candidate", base_branch="main"
    )
    storage.get_by_branch.return_value = storage.get.return_value
    storage.is_claimed_by_live_session.return_value = False
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "ok", 0)
    runner.run_storage.has_active_run_for_task.return_value = False
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
        lambda *_: {"id": "11111111-1111-4111-8111-111111110001", "project_path": str(source)},
    )
    repair = AsyncMock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.provider_mcp_config_error",
        lambda *_: None,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment", repair
    )
    monkeypatch.setattr("gobby.agents.isolation_worktree.repair_isolation_environment", repair)
    prepare = MagicMock(side_effect=RuntimeError("stopped after isolation"))
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.prepare_terminal_spawn", prepare
    )
    body = make_agent_definition(
        name=role,
        prompts=AgentPromptBlocks(agent="Observe the candidate"),
        task_step_writes=["review"] if role == "code-reviewer" else ["test"],
    )

    result = await spawn_agent_impl(
        prompt="join",
        runner=runner,
        agent_body=body,
        parent_session_id="parent",
        provider="codex",
        worktree_id="worktree" if reuse == "explicit" else None,
        checkout_mode="worktree",
        branch_name="candidate",
        worktree_storage=storage,
        git_manager=WorktreeGitManager(source),
        base_branch="main",
    )

    assert result["error"] == "stopped after isolation"
    prepare.assert_called_once()
    assert git(child, "rev-parse", "HEAD") == candidate
    assert git(child, "status", "--porcelain") == ""
    repair.assert_awaited_once()
    storage.update.assert_not_called()
