from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.worktrees._sync import create_sync_registry

pytestmark = pytest.mark.unit


@dataclass
class GitResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


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
