"""Regression tests for isolated-root project metadata and code indexing."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import CloneIsolationHandler, SpawnConfig, repair_isolation_environment
from gobby.code_index.trigger import CodeIndexTrigger

pytestmark = pytest.mark.unit


def _make_mock_proc() -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_clone_isolation_writes_parent_project_id(tmp_path: Path) -> None:
    """Clone isolation writes parent_project_id beside parent_project_path."""
    parent = tmp_path / "parent"
    (parent / ".gobby").mkdir(parents=True)
    (parent / ".gobby" / "project.json").write_text(
        json.dumps({"id": "parent-proj", "name": "parent"})
    )
    clone_path = tmp_path / "clone"

    clone_manager = MagicMock()

    def create_clone(**_kwargs: object) -> MagicMock:
        clone_path.mkdir()
        return MagicMock(success=True)

    clone_manager.create_clone.side_effect = create_clone
    clone_storage = MagicMock()
    clone_storage.get_by_branch.return_value = None
    clone_storage.create.return_value = MagicMock(
        id="clone-1",
        clone_path=str(clone_path),
        branch_name="feature",
    )

    handler = CloneIsolationHandler(clone_manager=clone_manager, clone_storage=clone_storage)
    handler._generate_clone_path = MagicMock(return_value=str(clone_path))
    config = SpawnConfig(
        prompt="Test",
        task_id=None,
        task_title=None,
        task_seq_num=None,
        branch_name="feature",
        branch_prefix=None,
        base_branch="main",
        project_id="parent-proj",
        project_path=str(parent),
        provider="codex",
        parent_session_id="sess-1",
    )

    with (
        patch("gobby.agents.isolation._copy_cli_hooks", new=AsyncMock()),
        patch("gobby.agents.isolation._patch_mcp_config_for_isolation", new=AsyncMock()),
    ):
        await handler.prepare_environment(config)

    data = json.loads((clone_path / ".gobby" / "project.json").read_text())
    assert data["id"] == "parent-proj"
    assert data["parent_project_path"] == str(parent.resolve())
    assert data["parent_project_id"] == "parent-proj"


@pytest.mark.asyncio
async def test_repair_marks_tracked_project_json_skip_worktree(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    worktree = tmp_path / "worktree"
    parent.mkdir()
    (parent / ".gobby").mkdir()
    (parent / ".gobby" / "project.json").write_text(
        json.dumps({"id": "parent-proj", "name": "parent"}) + "\n",
        encoding="utf-8",
    )
    _git(parent, "init", "-b", "main")
    _git(parent, "config", "user.email", "test@example.com")
    _git(parent, "config", "user.name", "Test User")
    _git(parent, "add", ".gobby/project.json")
    _git(parent, "commit", "-m", "initial")
    _git(parent, "worktree", "add", "-b", "isolation", str(worktree), "main")

    await repair_isolation_environment(
        main_repo_path=str(parent),
        isolated_path=str(worktree),
        provider="codex",
    )

    data = json.loads((worktree / ".gobby" / "project.json").read_text(encoding="utf-8"))
    assert data["parent_project_path"] == str(parent.resolve())
    assert data["parent_project_id"] == "parent-proj"
    assert _git(worktree, "status", "--porcelain").stdout == ""
    exclude_path = (
        worktree / _git(worktree, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    )
    assert ".mcp.json" in exclude_path.read_text(encoding="utf-8")
    assert _git(worktree, "ls-files", "-v", ".gobby/project.json").stdout.startswith("S ")


@pytest.mark.asyncio
async def test_repair_excludes_untracked_project_json(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    isolated = tmp_path / "isolated"
    parent.mkdir()
    isolated.mkdir()
    (parent / ".gobby").mkdir()
    (parent / ".gobby" / "project.json").write_text(
        json.dumps({"id": "parent-proj", "name": "parent"}) + "\n",
        encoding="utf-8",
    )
    _git(isolated, "init", "-b", "main")
    _git(isolated, "config", "user.email", "test@example.com")
    _git(isolated, "config", "user.name", "Test User")
    (isolated / "README.md").write_text("# isolated\n", encoding="utf-8")
    _git(isolated, "add", "README.md")
    _git(isolated, "commit", "-m", "initial")

    await repair_isolation_environment(
        main_repo_path=str(parent),
        isolated_path=str(isolated),
        provider="codex",
    )

    assert _git(isolated, "status", "--porcelain").stdout == ""
    exclude_path = (
        isolated / _git(isolated, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    )
    exclude_text = exclude_path.read_text(encoding="utf-8")
    assert ".mcp.json" in exclude_text
    assert ".gobby/project.json" in exclude_text


@pytest.mark.asyncio
async def test_clone_and_parent_can_index_same_relative_file_without_collision(
    tmp_path: Path,
) -> None:
    """Parent and clone roots sharing a parent id flush with distinct cwd values."""
    loop = asyncio.get_running_loop()
    trigger = CodeIndexTrigger(loop=loop, debounce_seconds=0.05)
    mock_proc = _make_mock_proc()
    parent = tmp_path / "parent"
    clone = tmp_path / "clone"
    parent.mkdir()
    clone.mkdir()

    with (
        patch("gobby.code_index.trigger.resolve_native_bin", return_value="/tmp/gcode"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        trigger._schedule_file("src/shared.py", "parent-proj", str(parent))
        trigger._schedule_file("src/shared.py", "parent-proj", str(clone))

        await trigger._flush(trigger._root_key(str(parent)), "parent-proj")
        await trigger._flush(trigger._root_key(str(clone)), "parent-proj")

    assert mock_exec.call_count == 2
    cwds = {call.kwargs["cwd"] for call in mock_exec.call_args_list}
    assert cwds == {str(parent.resolve()), str(clone.resolve())}
