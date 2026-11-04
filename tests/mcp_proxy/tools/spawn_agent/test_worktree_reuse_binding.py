"""Explicit worktree reuse: branch-name references and task binding."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import SpawnConfig
from gobby.agents.worktree_reuse import ReusedWorktreeSyncResult
from gobby.mcp_proxy.tools.spawn_agent._worktree_reuse import (
    prepare_reused_worktree,
    resolve_reused_worktree_id,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager, Worktree

pytestmark = pytest.mark.unit

_REUSE = "gobby.mcp_proxy.tools.spawn_agent._worktree_reuse"


def _worktree(
    storage: LocalWorktreeManager,
    project_id: str,
    path: Path,
    *,
    branch: str = "feature/reuse",
    task_id: str | None = None,
) -> Worktree:
    path.mkdir(exist_ok=True)
    return storage.create(
        project_id=project_id,
        branch_name=branch,
        worktree_path=str(path),
        base_branch="main",
        task_id=task_id,
    )


def test_reference_resolves_full_worktree_id(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    storage = LocalWorktreeManager(temp_db)
    worktree = _worktree(storage, sample_project["id"], tmp_path / "wt")

    assert resolve_reused_worktree_id(storage, sample_project["id"], worktree.id) == worktree.id


def test_reference_resolves_branch_name_to_project_worktree(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    storage = LocalWorktreeManager(temp_db)
    worktree = _worktree(storage, sample_project["id"], tmp_path / "wt", branch="task-7-reuse")

    resolved = resolve_reused_worktree_id(storage, sample_project["id"], "task-7-reuse")

    assert resolved == worktree.id


def test_reference_matching_neither_id_nor_branch_names_the_value(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    storage = LocalWorktreeManager(temp_db)
    _worktree(storage, sample_project["id"], tmp_path / "wt", branch="task-7-reuse")

    with pytest.raises(ValueError, match="no-such-branch") as excinfo:
        resolve_reused_worktree_id(storage, sample_project["id"], "no-such-branch")

    assert "neither a worktree id nor a branch name" in str(excinfo.value)


def _spawn_config(project_id: str, repo: Path, task_id: str) -> SpawnConfig:
    return SpawnConfig(
        prompt="continue",
        task_id=task_id,
        task_title="Reuse",
        task_seq_num=1,
        branch_name="feature/reuse",
        branch_prefix=None,
        base_branch="main",
        project_id=project_id,
        project_path=str(repo),
        provider="codex",
        parent_session_id="parent",
    )


def _git_manager() -> MagicMock:
    git_manager = MagicMock()
    git_manager.get_current_branch = AsyncMock(return_value="main")
    git_manager.get_local_commit = AsyncMock(return_value="base-sha")
    git_manager.run_git_command = AsyncMock(return_value=SimpleNamespace(returncode=0))
    return git_manager


@pytest.mark.asyncio
async def test_claimed_reuse_refuses_worktree_bound_to_another_task(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    tasks = LocalTaskManager(temp_db)
    bound = tasks.create_task(project_id=sample_project["id"], title="Bound task")
    other = tasks.create_task(project_id=sample_project["id"], title="Other task")
    storage = LocalWorktreeManager(temp_db)
    worktree = _worktree(storage, sample_project["id"], tmp_path / "wt", task_id=bound.id)
    sync = AsyncMock()

    with (
        patch(f"{_REUSE}.sync_reused_worktree_to_base", new=sync),
        patch(f"{_REUSE}.repair_isolation_environment", new=AsyncMock()),
        pytest.raises(RuntimeError, match="bound to task") as excinfo,
    ):
        await prepare_reused_worktree(
            existing_worktree=worktree,
            git_manager=_git_manager(),
            worktree_storage=storage,
            spawn_config=_spawn_config(sample_project["id"], tmp_path / "repo", other.id),
            main_repo_path=str(tmp_path / "repo"),
        )

    assert bound.id in str(excinfo.value)
    assert other.id in str(excinfo.value)
    sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_claimed_reuse_allows_worktree_bound_to_same_task(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(project_id=sample_project["id"], title="Bound task")
    storage = LocalWorktreeManager(temp_db)
    worktree = _worktree(storage, sample_project["id"], tmp_path / "wt", task_id=task.id)
    sync = AsyncMock(
        return_value=ReusedWorktreeSyncResult(
            status="already_current", base_ref="main", base_commit_sha="base-sha"
        )
    )

    with (
        patch(f"{_REUSE}.sync_reused_worktree_to_base", new=sync),
        patch(f"{_REUSE}.repair_isolation_environment", new=AsyncMock()),
    ):
        context, _ = await prepare_reused_worktree(
            existing_worktree=worktree,
            git_manager=_git_manager(),
            worktree_storage=storage,
            spawn_config=_spawn_config(sample_project["id"], tmp_path / "repo", task.id),
            main_repo_path=str(tmp_path / "repo"),
        )

    assert context.worktree_id == worktree.id
    assert context.cwd == worktree.worktree_path
    sync.assert_awaited_once()
