"""Tests that isolation handlers persist base_commit_sha before agent setup."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import CloneIsolationHandler, SpawnConfig, WorktreeIsolationHandler
from gobby.storage.tasks import LocalTaskManager, TaskArtifactManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_worktree_handler_captures_base(temp_db, sample_project, tmp_path: Path) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Worktree base",
    )
    handler, worktree_path = _worktree_handler(temp_db, tmp_path)

    await _prepare_with_git_head(handler, _config(task.id, sample_project["id"], tmp_path))

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.worktree_path == worktree_path
    assert artifacts.worktree_id == "wt-123"
    assert artifacts.base_commit_sha == "abc123"


@pytest.mark.asyncio
async def test_clone_handler_captures_base(temp_db, sample_project, tmp_path: Path) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Clone base",
    )
    handler, clone_path = _clone_handler(temp_db, tmp_path)

    await _prepare_with_git_head(handler, _config(task.id, sample_project["id"], tmp_path))

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.clone_path == clone_path
    assert artifacts.clone_id == "clone-123"
    assert artifacts.base_commit_sha == "abc123"


@pytest.mark.asyncio
async def test_base_captured_before_first_agent_run(
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Ordering",
    )
    handler, _worktree_path = _worktree_handler(temp_db, tmp_path)

    async def assert_base_before_hook_copy(*_args: object, **_kwargs: object) -> None:
        artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
        assert artifacts.base_commit_sha == "abc123"

    with (
        patch(
            "gobby.agents.isolation._copy_cli_hooks",
            new=AsyncMock(side_effect=assert_base_before_hook_copy),
        ),
        patch("gobby.agents.isolation._patch_mcp_config_for_isolation", new=AsyncMock()),
        patch("gobby.agents.isolation.subprocess.run", return_value=_git_head("abc123")),
    ):
        await handler.prepare_environment(_config(task.id, sample_project["id"], tmp_path))


async def _prepare_with_git_head(handler, config: SpawnConfig) -> None:
    with (
        patch("gobby.agents.isolation._copy_cli_hooks", new=AsyncMock()),
        patch("gobby.agents.isolation._patch_mcp_config_for_isolation", new=AsyncMock()),
        patch("gobby.agents.isolation.subprocess.run", return_value=_git_head("abc123")) as run,
    ):
        await handler.prepare_environment(config)

    run.assert_called_once()
    assert run.call_args.args[0][-2:] == ["rev-parse", "HEAD"]


def _worktree_handler(temp_db, tmp_path: Path) -> tuple[WorktreeIsolationHandler, str]:
    worktree_path = str(tmp_path / "wt")
    git_manager = MagicMock()
    git_manager.repo_path = str(tmp_path / "repo")
    git_manager.get_current_branch.return_value = "main"
    git_manager.has_unpushed_commits.return_value = (False, 0)
    git_manager.create_worktree.return_value = MagicMock(success=True)
    storage = MagicMock()
    storage.db = temp_db
    storage.get_by_branch.return_value = None
    storage.create.return_value = MagicMock(
        id="wt-123",
        worktree_path=worktree_path,
        branch_name="branch",
    )
    handler = WorktreeIsolationHandler(git_manager=git_manager, worktree_storage=storage)
    handler._generate_worktree_path = MagicMock(return_value=worktree_path)  # type: ignore[method-assign]
    return handler, worktree_path


def _clone_handler(temp_db, tmp_path: Path) -> tuple[CloneIsolationHandler, str]:
    clone_path = str(tmp_path / "clone")
    clone_manager = MagicMock()
    clone_manager.create_clone.return_value = MagicMock(success=True)
    storage = MagicMock()
    storage.db = temp_db
    storage.get_by_branch.return_value = None
    storage.create.return_value = MagicMock(
        id="clone-123",
        clone_path=clone_path,
        branch_name="branch",
    )
    handler = CloneIsolationHandler(clone_manager=clone_manager, clone_storage=storage)
    handler._generate_clone_path = MagicMock(return_value=clone_path)  # type: ignore[method-assign]
    return handler, clone_path


def _config(task_id: str, project_id: str, tmp_path: Path) -> SpawnConfig:
    return SpawnConfig(
        prompt="Test",
        task_id=task_id,
        task_title="Task",
        task_seq_num=13260,
        branch_name="branch",
        branch_prefix=None,
        base_branch="main",
        project_id=project_id,
        project_path=str(tmp_path / "repo"),
        provider="claude",
        parent_session_id="session",
    )


def _git_head(sha: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git", "rev-parse", "HEAD"],
        returncode=0,
        stdout=f"{sha}\n",
        stderr="",
    )
