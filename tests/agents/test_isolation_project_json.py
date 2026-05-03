"""Regression tests for isolated-root project metadata and code indexing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import CloneIsolationHandler, SpawnConfig
from gobby.code_index.trigger import CodeIndexTrigger

pytestmark = pytest.mark.unit


def _make_mock_proc() -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


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

        await asyncio.sleep(0.1)

    assert mock_exec.call_count == 2
    cwds = {call.kwargs["cwd"] for call in mock_exec.call_args_list}
    assert cwds == {str(parent.resolve()), str(clone.resolve())}
