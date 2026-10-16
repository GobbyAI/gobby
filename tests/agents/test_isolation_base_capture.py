"""Tests that isolation handlers persist base_commit_sha before agent setup."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import (
    CloneIsolationHandler,
    IsolationContext,
    SpawnConfig,
    WorktreeIsolationHandler,
    repair_isolation_environment,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, TaskArtifactManager
from gobby.utils.project_context import IsolationProjectJsonError

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_worktree_handler_captures_selected_commit(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Worktree base",
        validation_criteria=(
            "Worktree isolation records the selected commit in task artifacts and runtime context."
        ),
    )
    handler, worktree_path = _worktree_handler(temp_db, tmp_path, monkeypatch)

    ctx = await _prepare_worktree_with_selected_commit(
        handler,
        _config(task.id, sample_project["id"], tmp_path),
    )

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.worktree_path == worktree_path
    assert artifacts.worktree_id == "eeeeeeee-eeee-4eee-8eee-eeeeeeee1123"
    assert artifacts.base_commit_sha == "base123"
    assert ctx.extra["base_commit_sha"] == "base123"


@pytest.mark.asyncio
async def test_clone_handler_captures_base(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Clone base",
        validation_criteria=(
            "Clone isolation records the base commit in task artifacts and runtime context."
        ),
    )
    handler, clone_path = _clone_handler(temp_db, tmp_path, monkeypatch)
    git_manager = handler._git_manager
    assert git_manager is not None

    with (
        patch("gobby.agents.isolation_repair._copy_cli_hooks", new=AsyncMock()),
        patch("gobby.agents.isolation_repair._patch_mcp_config_for_isolation", new=AsyncMock()),
    ):
        ctx = await handler.prepare_environment(_config(task.id, sample_project["id"], tmp_path))

    git_manager.get_local_commit.assert_awaited_once_with("refs/heads/main^{commit}")
    handler._clone_manager.create_clone.assert_awaited_once_with(
        clone_path=clone_path,
        branch_name=ctx.branch_name,
        base_branch="main",
        base_commit_sha="abc123",
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.clone_path == clone_path
    assert artifacts.clone_id == "cccccccc-cccc-4ccc-8ccc-cccccccc1123"
    assert artifacts.base_commit_sha == "abc123"
    assert ctx.extra["base_commit_sha"] == "abc123"


@pytest.mark.asyncio
async def test_base_captured_before_first_agent_run(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Ordering",
        validation_criteria=(
            "Isolation persists the base commit before copying agent runtime hooks."
        ),
    )
    handler, _worktree_path = _worktree_handler(temp_db, tmp_path, monkeypatch)
    handler._git_manager.run_git_command.return_value = _git_head("abc123")
    handler._git_manager.get_local_commit.return_value = "abc123"

    async def assert_base_before_hook_copy(*_args: object, **_kwargs: object) -> None:
        artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
        assert artifacts.base_commit_sha == "abc123"

    with (
        patch(
            "gobby.agents.isolation_repair._copy_cli_hooks",
            new=AsyncMock(side_effect=assert_base_before_hook_copy),
        ),
        patch("gobby.agents.isolation_repair._patch_mcp_config_for_isolation", new=AsyncMock()),
    ):
        await handler.prepare_environment(_config(task.id, sample_project["id"], tmp_path))

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.worktree_id == "eeeeeeee-eeee-4eee-8eee-eeeeeeee1123"


@pytest.mark.asyncio
async def test_repair_isolation_environment_logs_git_hygiene_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Git hygiene failures are logged and do not block isolation repair."""
    with (
        patch("gobby.agents.isolation_repair._copy_cli_hooks", new=AsyncMock()),
        patch("gobby.agents.isolation_repair._patch_mcp_config_for_isolation", new=AsyncMock()),
        patch("gobby.utils.project_context.ensure_project_json_for_isolation"),
        patch(
            "gobby.agents.isolation_repair.preseed_isolated_python_environment",
            new=AsyncMock(return_value=SimpleNamespace(attempted=False, success=True)),
        ),
        patch(
            "gobby.agents.isolation_repair.apply_isolation_git_hygiene",
            side_effect=RuntimeError("git hygiene failed"),
        ) as hygiene,
        caplog.at_level(logging.WARNING, logger="gobby.agents.isolation"),
    ):
        timings: dict[str, float] = {}
        await repair_isolation_environment(
            main_repo_path="/tmp/main",
            isolated_path="/tmp/isolated",
            provider="codex",
            phase_timings_ms=timings,
        )

    hygiene.assert_called_once_with("/tmp/isolated", main_repo_path="/tmp/main")
    assert "isolation_git_hygiene" in timings
    assert "Failed to apply isolation git hygiene for /tmp/isolated" in caplog.text


@pytest.mark.asyncio
async def test_repair_isolation_environment_propagates_project_json_failure() -> None:
    """Project metadata failures stop the remaining isolation repair steps."""
    error = IsolationProjectJsonError("metadata failed")
    patch_mcp = AsyncMock()
    with (
        patch("gobby.agents.isolation_repair._copy_cli_hooks", new=AsyncMock()),
        patch("gobby.agents.isolation_repair._patch_mcp_config_for_isolation", new=patch_mcp),
        patch(
            "gobby.utils.project_context.ensure_project_json_for_isolation",
            side_effect=error,
        ),
    ):
        with pytest.raises(IsolationProjectJsonError, match="metadata failed") as exc_info:
            await repair_isolation_environment(
                main_repo_path="/tmp/main",
                isolated_path="/tmp/isolated",
                provider="codex",
            )

    assert exc_info.value is error
    patch_mcp.assert_not_awaited()


async def _prepare_worktree_with_selected_commit(
    handler: WorktreeIsolationHandler,
    config: SpawnConfig,
) -> IsolationContext:
    handler._git_manager.run_git_command.return_value = _git_head("base123")
    handler._git_manager.get_local_commit.return_value = "base123"
    with (
        patch("gobby.agents.isolation_repair._copy_cli_hooks", new=AsyncMock()),
        patch("gobby.agents.isolation_repair._patch_mcp_config_for_isolation", new=AsyncMock()),
    ):
        ctx = await handler.prepare_environment(config)

    handler._git_manager.get_local_commit.assert_awaited_once_with("refs/heads/main^{commit}")
    handler._git_manager.create_worktree.assert_awaited_once_with(
        worktree_path=ctx.cwd,
        branch_name=ctx.branch_name,
        base_branch="main",
        base_commit_sha="base123",
        create_branch=True,
    )
    return ctx


def _worktree_handler(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[WorktreeIsolationHandler, str]:
    from gobby.worktrees.git import WorktreeGitManager

    worktree_path = str(tmp_path / "wt")
    git_manager = MagicMock(spec=WorktreeGitManager)
    git_manager.repo_path = str(tmp_path / "repo")
    git_manager.get_current_branch.return_value = "main"
    git_manager.has_unpushed_commits.return_value = (False, 0)
    git_manager.create_worktree.return_value = MagicMock(success=True)
    storage = MagicMock()
    storage.db = temp_db
    storage.get_by_branch.return_value = None
    storage.create.return_value = MagicMock(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeee1123",
        worktree_path=worktree_path,
        branch_name="branch",
    )
    handler = WorktreeIsolationHandler(git_manager=git_manager, worktree_storage=storage)
    monkeypatch.setattr(handler, "_generate_worktree_path", MagicMock(return_value=worktree_path))
    return handler, worktree_path


def _clone_handler(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CloneIsolationHandler, str]:
    from gobby.worktrees.git import WorktreeGitManager

    clone_path = str(tmp_path / "clone")
    git_manager = MagicMock(spec=WorktreeGitManager)
    git_manager.run_git_command.return_value = _git_head("abc123")
    git_manager.get_local_commit.return_value = "abc123"
    clone_manager = MagicMock()
    clone_manager.create_clone = AsyncMock(return_value=MagicMock(success=True))
    storage = MagicMock()
    storage.db = temp_db
    storage.get_by_branch.return_value = None
    storage.create.return_value = MagicMock(
        id="cccccccc-cccc-4ccc-8ccc-cccccccc1123",
        clone_path=clone_path,
        branch_name="branch",
    )
    handler = CloneIsolationHandler(
        clone_manager=clone_manager, clone_storage=storage, git_manager=git_manager
    )
    monkeypatch.setattr(handler, "_generate_clone_path", MagicMock(return_value=clone_path))
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
