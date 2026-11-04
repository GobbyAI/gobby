"""A claiming resume keeps its own checkout but never moves a tree onto another task."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.workflows.agent_models import AgentPromptBlocks
from gobby.worktrees.git import WorktreeGitManager
from tests.fixtures.agent_definitions import make_agent_definition
from tests.mcp_proxy.tools.spawn_agent.test_associated_worktree_join import git

pytestmark = pytest.mark.usefixtures("stub_srt_verifier")

STOPPED = "stopped after isolation"


@pytest.mark.integration
@pytest.mark.parametrize("target", ["same-task", "other-task"])
async def test_claiming_resume_refuses_a_tree_bound_to_another_task(
    tmp_path: Path,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Resume Test")
    git(source, "commit", "--allow-empty", "-m", "initial")
    child = tmp_path / "seat-tree"
    git(source, "worktree", "add", "-b", "seat-a", str(child))
    (child / "in-flight.txt").write_text("uncommitted\n")
    (source / "base.txt").write_text("base\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base advances")
    seat_head = git(child, "rev-parse", "HEAD")

    tasks = LocalTaskManager(temp_db)
    task_a, task_b = (
        tasks.create_task(
            project_id=sample_project["id"],
            title=title,
            validation_criteria="Resume keeps the seat's tree on its own task.",
        )
        for title in ("Seat task", "Free task")
    )
    storage = LocalWorktreeManager(temp_db)
    tree = storage.create(
        project_id=sample_project["id"],
        branch_name="seat-a",
        worktree_path=str(child),
        base_branch="main",
        task_id=task_a.id,
    )
    seat = SimpleNamespace(
        id="seat-a-session",
        external_id="native-seat-thread",
        agent_run_id=None,
        parent_session_id="parent",
        status="expired",
        workflow_name=None,
        workspace_path=None,
    )
    impl = "gobby.mcp_proxy.tools.spawn_agent._implementation"
    monkeypatch.setattr(f"{impl}.resolve_seat_resume", AsyncMock(return_value=seat))
    monkeypatch.setattr(
        f"{impl}.resume_launch_defaults",
        lambda _target, *, model, reasoning_effort: ("codex", model, reasoning_effort),
    )
    monkeypatch.setattr(
        f"{impl}.get_project_context",
        lambda *_: {"id": sample_project["id"], "project_path": str(source)},
    )
    monkeypatch.setattr(f"{impl}.provider_mcp_config_error", lambda *_: None)
    repair = AsyncMock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse.repair_isolation_environment", repair
    )
    monkeypatch.setattr("gobby.agents.isolation_worktree.repair_isolation_environment", repair)
    prepare = MagicMock(side_effect=RuntimeError(STOPPED))
    monkeypatch.setattr("gobby.agents.spawn.prepare_terminal_resume", prepare)
    monkeypatch.setattr(f"{impl}.prepare_terminal_spawn", prepare)
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "ok", 0)
    runner.run_storage.has_active_run_for_task.return_value = False

    result = await spawn_agent_impl(
        prompt="continue",
        runner=runner,
        agent_body=make_agent_definition(
            name="default",
            prompts=AgentPromptBlocks(agent="Continue development"),
            model="gpt-5.4",
            task_attestation_writes=[],
        ),
        task_id=task_a.id if target == "same-task" else task_b.id,
        task_manager=tasks,
        parent_session_id="parent",
        provider="codex",
        resume_session_id=seat.id,
        worktree_id=tree.id,
        checkout_mode="worktree",
        worktree_storage=storage,
        git_manager=WorktreeGitManager(source),
        base_branch="main",
        claim=True,
    )

    # Either way the seat's checkout is exactly as it left it: no sync onto the base.
    assert git(child, "rev-parse", "HEAD") == seat_head
    assert (child / "in-flight.txt").read_text() == "uncommitted\n"
    if target == "same-task":
        assert result["error"] == STOPPED
        repair.assert_awaited_once()
        prepare.assert_called_once()
    else:
        assert result["success"] is False
        assert f"bound to task {task_a.id}, not the spawn's task {task_b.id}" in result["error"]
        repair.assert_not_awaited()
        prepare.assert_not_called()
