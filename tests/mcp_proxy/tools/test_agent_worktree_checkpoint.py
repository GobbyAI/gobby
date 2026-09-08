"""Coordinator authorization tests for agent worktree checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.worktree_checkpoint import WorktreeCheckpoint, WorktreeCheckpointError
from gobby.mcp_proxy.tools.agents_checkpoint_tools import register_agent_checkpoint_tools
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.workflows.commit_guard import CheckoutPathOwnership, ForeignPathOwner


@dataclass(frozen=True)
class _CheckpointHarness:
    registry: InternalToolRegistry
    ctx: AgentsRegistryContext
    agent_runs: MagicMock
    task_manager: MagicMock
    worktrees: MagicMock


def _checkpoint_registry(tmp_path: Path) -> _CheckpointHarness:
    run = SimpleNamespace(
        id="run-1",
        status="error",
        parent_session_id="parent",
        child_session_id="child",
        task_id="task-1",
        worktree_id="wt-1",
        machine_id="machine-1",
    )
    task = SimpleNamespace(
        id="task-1",
        seq_num=42,
        project_id="project-1",
        claimed_by_session_id="child",
        closed_at=None,
    )
    worktree = SimpleNamespace(
        id="wt-1",
        task_id="task-1",
        project_id="project-1",
        workspace_role="task",
        status="active",
        machine_id="machine-1",
        agent_session_id=None,
        worktree_path=str(tmp_path / "worktree"),
        branch_name="task-42",
    )
    agent_runs = MagicMock()
    agent_runs.get.return_value = run
    agent_runs.get_active_run_for_task.return_value = None
    agent_runs.get_active_run_for_worktree.return_value = None
    task_manager = MagicMock()
    task_manager.get_task.return_value = task
    worktrees = MagicMock()
    worktrees.get.return_value = worktree
    worktrees.claim_if_available.return_value = worktree
    worktrees.release.return_value = worktree
    git_manager = MagicMock()
    git_manager.inspect_worktree.return_value = SimpleNamespace(
        path=worktree.worktree_path,
        branch=worktree.branch_name,
        is_bare=False,
        is_detached=False,
        prunable=False,
    )
    ctx = AgentsRegistryContext(
        runner=MagicMock(),
        agent_run_manager=agent_runs,
        resolve_session_id=lambda session_id: session_id,
        get_current_session_id=lambda: "parent",
        get_current_agent_run_id=lambda: None,
        get_project_context=lambda: None,
        task_manager=task_manager,
        worktree_storage=worktrees,
        git_manager=git_manager,
        db=MagicMock(),
    )
    registry = InternalToolRegistry(name="test-agents", description="test")
    register_agent_checkpoint_tools(registry, ctx)
    return _CheckpointHarness(registry, ctx, agent_runs, task_manager, worktrees)


@pytest.mark.asyncio
async def test_parent_coordinator_checkpoints_and_releases_worktree(tmp_path: Path) -> None:
    harness = _checkpoint_registry(tmp_path)
    ownership = [CheckoutPathOwnership("owned.py", True, False, ())]
    checkpoint = WorktreeCheckpoint(
        commit_sha="abc123",
        included_paths=("owned.py",),
        message="checkpoint message",
    )

    with (
        patch(
            "gobby.mcp_proxy.tools.agents_checkpoint_tools.inspect_checkout_path_ownership",
            return_value=ownership,
        ),
        patch(
            "gobby.mcp_proxy.tools.agents_checkpoint_tools._authorized_task_paths",
            return_value={"owned.py"},
        ),
        patch(
            "gobby.mcp_proxy.tools.agents_checkpoint_tools.checkpoint_worktree",
            return_value=checkpoint,
        ) as checkpoint_mock,
    ):
        result = await harness.registry._tools["checkpoint_agent_worktree"].func(run_id="run-1")

    assert result["success"] is True
    assert result["commit_sha"] == "abc123"
    assert result["included_paths"] == ["owned.py"]
    harness.worktrees.claim_if_available.assert_called_once_with(
        "wt-1",
        "parent",
        allowed_existing_session_ids=("parent", "child"),
    )
    harness.worktrees.release.assert_called_once_with("wt-1")
    harness.task_manager.release_task_claim.assert_not_called()
    checkpoint_mock.assert_called_once()


@pytest.mark.asyncio
async def test_active_run_is_refused_before_worktree_claim(tmp_path: Path) -> None:
    harness = _checkpoint_registry(tmp_path)
    harness.agent_runs.get.return_value.status = "running"

    result = await harness.registry._tools["checkpoint_agent_worktree"].func(run_id="run-1")

    assert result["error_code"] == "run_active"
    harness.worktrees.claim_if_available.assert_not_called()


@pytest.mark.asyncio
async def test_non_parent_caller_is_refused(tmp_path: Path) -> None:
    harness = _checkpoint_registry(tmp_path)
    harness.ctx.get_current_session_id = lambda: "other"

    result = await harness.registry._tools["checkpoint_agent_worktree"].func(run_id="run-1")

    assert result["error_code"] == "coordinator_required"
    harness.worktrees.claim_if_available.assert_not_called()


@pytest.mark.asyncio
async def test_shared_or_non_task_checkout_is_refused(tmp_path: Path) -> None:
    harness = _checkpoint_registry(tmp_path)
    harness.agent_runs.get.return_value.worktree_id = None

    shared_result = await harness.registry._tools["checkpoint_agent_worktree"].func(run_id="run-1")

    assert shared_result["error_code"] == "isolated_worktree_required"
    harness.agent_runs.get.return_value.worktree_id = "wt-1"
    harness.worktrees.get.return_value.workspace_role = "integration"

    role_result = await harness.registry._tools["checkpoint_agent_worktree"].func(run_id="run-1")

    assert role_result["error_code"] == "isolated_worktree_required"
    harness.worktrees.claim_if_available.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_task_and_worktree_ownership_are_refused(tmp_path: Path) -> None:
    harness = _checkpoint_registry(tmp_path)
    harness.task_manager.get_task.return_value.claimed_by_session_id = "foreign"

    task_result = await harness.registry._tools["checkpoint_agent_worktree"].func(run_id="run-1")

    assert task_result["error_code"] == "foreign_task_owner"
    harness.task_manager.get_task.return_value.claimed_by_session_id = "child"
    harness.worktrees.claim_if_available.return_value = None

    worktree_result = await harness.registry._tools["checkpoint_agent_worktree"].func(
        run_id="run-1"
    )

    assert worktree_result["error_code"] == "foreign_worktree_owner"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ownership", "authorized", "error_code"),
    [
        (
            [
                CheckoutPathOwnership(
                    "foreign.py",
                    True,
                    False,
                    (ForeignPathOwner("foreign.py", "#99", "#99"),),
                )
            ],
            {"foreign.py"},
            "foreign_attributed_paths",
        ),
        (
            [CheckoutPathOwnership("unowned.py", True, False, ())],
            set(),
            "unattributed_paths",
        ),
    ],
)
async def test_path_ownership_failures_release_worktree(
    tmp_path: Path,
    ownership: list[CheckoutPathOwnership],
    authorized: set[str],
    error_code: str,
) -> None:
    harness = _checkpoint_registry(tmp_path)

    with (
        patch(
            "gobby.mcp_proxy.tools.agents_checkpoint_tools.inspect_checkout_path_ownership",
            return_value=ownership,
        ),
        patch(
            "gobby.mcp_proxy.tools.agents_checkpoint_tools._authorized_task_paths",
            return_value=authorized,
        ),
        patch("gobby.mcp_proxy.tools.agents_checkpoint_tools.checkpoint_worktree") as checkpoint,
    ):
        result = await harness.registry._tools["checkpoint_agent_worktree"].func(run_id="run-1")

    assert result["error_code"] == error_code
    harness.worktrees.release.assert_called_once_with("wt-1")
    checkpoint.assert_not_called()


@pytest.mark.asyncio
async def test_commit_failure_releases_worktree_without_releasing_task_claim(
    tmp_path: Path,
) -> None:
    harness = _checkpoint_registry(tmp_path)
    ownership = [CheckoutPathOwnership("owned.py", True, False, ())]

    with (
        patch(
            "gobby.mcp_proxy.tools.agents_checkpoint_tools.inspect_checkout_path_ownership",
            return_value=ownership,
        ),
        patch(
            "gobby.mcp_proxy.tools.agents_checkpoint_tools._authorized_task_paths",
            return_value={"owned.py"},
        ),
        patch(
            "gobby.mcp_proxy.tools.agents_checkpoint_tools.checkpoint_worktree",
            side_effect=WorktreeCheckpointError("commit failed"),
        ),
    ):
        result = await harness.registry._tools["checkpoint_agent_worktree"].func(run_id="run-1")

    assert result["error_code"] == "checkpoint_failed"
    harness.worktrees.release.assert_called_once_with("wt-1")
    harness.task_manager.release_task_claim.assert_not_called()
