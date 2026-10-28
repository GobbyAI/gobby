"""Workspace backups remain responsive and exclude generated trees at discovery."""

from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from gobby.agents import agent_workspaces

pytestmark = pytest.mark.unit


@pytest.fixture
def project_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    return str(uuid.uuid4())


async def test_workspace_scan_keeps_calling_loop_responsive(
    project_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = agent_workspaces.ensure_workspace(project_id)
    (anchor / "notes.md").write_text("evidence\n")
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    real_filter = agent_workspaces._eligible_paths

    def blocked_filter(root: Path, paths: list[str]) -> list[str]:
        entered.set()
        release.wait(timeout=2)
        try:
            return real_filter(root, paths)
        finally:
            returned.set()

    monkeypatch.setattr(agent_workspaces, "_eligible_paths", blocked_filter)
    commit = asyncio.create_task(agent_workspaces.commit_workspace(project_id, "timer"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # This coroutine must run while filtering remains blocked in the worker.
        assert not returned.is_set(), "workspace filtering blocked the calling event loop"
    finally:
        release.set()
        await commit


async def test_generated_trees_are_absent_from_git_status(
    project_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = agent_workspaces.ensure_workspace(project_id)
    folders = (
        "developer/session-1/target-gclient/debug/incremental",
        "developer/session-1/node_modules/package",
        "orchestrator/session-2/diag/.codex/plugins/cache",
        "developer/session-3/.venv/lib",
        "test-writer/session-4/gobby-pytest-home-abc/logs",
    )
    for folder in folders:
        root = anchor / folder
        root.mkdir(parents=True)
        for index in range(30):
            (root / f"artifact-{index}.bin").write_bytes(b"build output")
    (anchor / "notes.md").write_text("keep evidence\n")
    statuses: list[str] = []
    real_git = agent_workspaces._git

    async def capture(project: str, *args: str, **kwargs: Any) -> Any:
        result = await real_git(project, *args, **kwargs)
        if args[0] == "status":
            statuses.append(result.stdout)
        return result

    monkeypatch.setattr(agent_workspaces, "_git", capture)
    assert await agent_workspaces.commit_workspace(project_id, "pre_compact")
    assert statuses
    assert all("artifact-" not in status for status in statuses)
    assert any("notes.md" in status for status in statuses)
