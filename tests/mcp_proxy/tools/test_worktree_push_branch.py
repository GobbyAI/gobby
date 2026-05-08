from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry
from tests.mcp_proxy.tools.git_helpers import GitResult

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_push_branch_pushes_worktree_branch_with_force_lease() -> None:
    git_manager = MagicMock()
    git_manager.run_git_command.return_value = GitResult(0, stdout="ok")
    worktree_storage = MagicMock()
    worktree_storage.get.return_value = SimpleNamespace(
        worktree_path="/repo/.worktrees/wt-1",
        branch_name="feature/task",
    )
    ctx = SimpleNamespace(
        worktree_storage=worktree_storage,
        git_manager=git_manager,
        project_id="project-1",
    )

    result = await create_sync_registry(ctx).call(
        "push_branch",
        {
            "worktree_id": "wt-1",
            "target_branch": "feature/task",
            "force_with_lease": True,
        },
    )

    assert result["success"] is True
    git_manager.run_git_command.assert_called_once_with(
        [
            "push",
            "--no-verify",
            "--force-with-lease",
            "origin",
            "feature/task:feature/task",
        ],
        cwd="/repo/.worktrees/wt-1",
        timeout=60,
    )


@pytest.mark.asyncio
async def test_push_branch_omits_force_lease_by_default() -> None:
    git_manager = MagicMock()
    git_manager.run_git_command.return_value = GitResult(0, stdout="ok")
    worktree_storage = MagicMock()
    worktree_storage.get.return_value = SimpleNamespace(
        worktree_path="/repo/.worktrees/wt-1",
        branch_name="feature/task",
    )
    ctx = SimpleNamespace(
        worktree_storage=worktree_storage,
        git_manager=git_manager,
        project_id="project-1",
    )

    result = await create_sync_registry(ctx).call(
        "push_branch",
        {"worktree_id": "wt-1", "target_branch": "feature/task"},
    )

    assert result["success"] is True
    git_manager.run_git_command.assert_called_once_with(
        ["push", "--no-verify", "origin", "feature/task:feature/task"],
        cwd="/repo/.worktrees/wt-1",
        timeout=60,
    )


@pytest.mark.asyncio
async def test_push_branch_reports_missing_worktree() -> None:
    git_manager = MagicMock()
    worktree_storage = MagicMock()
    worktree_storage.get.return_value = None
    ctx = SimpleNamespace(
        worktree_storage=worktree_storage,
        git_manager=git_manager,
        project_id="project-1",
    )

    result = await create_sync_registry(ctx).call("push_branch", {"worktree_id": "missing"})

    assert result == {"success": False, "error": "Worktree 'missing' not found"}
    git_manager.run_git_command.assert_not_called()


@pytest.mark.asyncio
async def test_push_branch_returns_push_failure() -> None:
    git_manager = MagicMock()
    git_manager.run_git_command.return_value = GitResult(1, stderr="rejected")
    worktree_storage = MagicMock()
    worktree_storage.get.return_value = SimpleNamespace(
        worktree_path="/repo/.worktrees/wt-1",
        branch_name="feature/task",
    )
    ctx = SimpleNamespace(
        worktree_storage=worktree_storage,
        git_manager=git_manager,
        project_id="project-1",
    )

    result = await create_sync_registry(ctx).call(
        "push_branch",
        {"worktree_id": "wt-1", "target_branch": "feature/task"},
    )

    assert result["success"] is False
    assert result["error"] == "rejected"
    assert result["stderr"] == "rejected"


@pytest.mark.asyncio
async def test_push_branch_uses_custom_remote_and_source_branch() -> None:
    git_manager = MagicMock()
    git_manager.run_git_command.return_value = GitResult(0, stdout="ok")
    worktree_storage = MagicMock()
    worktree_storage.get.return_value = SimpleNamespace(
        worktree_path="/repo/.worktrees/wt-1",
        branch_name="feature/task",
    )
    ctx = SimpleNamespace(
        worktree_storage=worktree_storage,
        git_manager=git_manager,
        project_id="project-1",
    )

    result = await create_sync_registry(ctx).call(
        "push_branch",
        {
            "worktree_id": "wt-1",
            "branch": "local/topic",
            "target_branch": "review/topic",
            "remote": "fork",
        },
    )

    assert result["success"] is True
    git_manager.run_git_command.assert_called_once_with(
        ["push", "--no-verify", "fork", "local/topic:review/topic"],
        cwd="/repo/.worktrees/wt-1",
        timeout=60,
    )
