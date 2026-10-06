"""set_landing_freeze stores the freeze in the project's git common dir."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.landing_policy import read_freeze
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_set_landing_freeze_records_setter_and_clearer(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    subprocess.run(["git", "init", "-q", isolated.root_path], check=True)
    sessions = SessionManager(temp_db)
    setter, clearer = (
        sessions.register(
            external_id=external_id,
            machine_id=isolated.machine_id,
            source="codex",
            project_id=isolated.project.id,
        )
        for external_id in ("freeze-setter", "freeze-clearer")
    )
    registry = create_task_ops_registry(LocalTaskManager(temp_db))
    common_dir = Path(isolated.root_path) / ".git"

    unsessioned = await registry.call("set_landing_freeze", {"on": True, "reason": "release"})
    with session_context_for_test(setter.id):
        blank = await registry.call("set_landing_freeze", {"on": True, "reason": "  "})
        assert read_freeze(common_dir).on is False
        frozen = await registry.call("set_landing_freeze", {"on": True, "reason": "release"})
    stored_on = read_freeze(common_dir)
    with session_context_for_test(clearer.id):
        cleared = await registry.call("set_landing_freeze", {"on": False, "reason": "shipped"})
    stored_off = read_freeze(common_dir)

    assert unsessioned["success"] is False
    assert blank["success"] is False
    assert frozen["success"] is True
    assert frozen["path"] == str(common_dir / "gobby" / "landing-freeze.json")
    assert (stored_on.on, stored_on.reason, stored_on.set_by_session_id) == (
        True,
        "release",
        setter.id,
    )
    assert cleared["success"] is True
    assert (stored_off.on, stored_off.reason, stored_off.set_by_session_id) == (
        False,
        "shipped",
        clearer.id,
    )
    assert cleared["freeze"]["set_by_session_id"] == clearer.id


@pytest.mark.asyncio
async def test_land_commit_requires_session_and_forwards_resolved_task(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    caller = SessionManager(temp_db).register(
        external_id="landing-reviewer",
        machine_id=isolated.machine_id,
        source="codex",
        project_id=isolated.project.id,
    )
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        isolated.project.id,
        "Candidate",
        created_in_session_id=caller.id,
        validation_criteria="Forward the resolved task and candidate SHA to the landing service.",
    )
    registry = create_task_ops_registry(tasks)
    sha = "a" * 40
    response = {"landed": True, "mode": "ff", "landed_tip": sha}
    service = AsyncMock(return_value=response)
    with patch("gobby.mcp_proxy.tools.tasks._landing.land_candidate", service):
        unsessioned = await registry.call("land_commit", {"task_id": task.id, "commit_sha": sha})
        service.assert_not_awaited()
        with session_context_for_test(caller.id):
            missing = await registry.call("land_commit", {"task_id": "#999999", "commit_sha": sha})
            service.assert_not_awaited()
            landed = await registry.call(
                "land_commit",
                {
                    "task_id": f"#{task.seq_num}",
                    "commit_sha": sha,
                },
            )
    assert unsessioned == {"landed": False, "error": "session_required"}
    assert missing["landed"] is False
    assert landed == response
    service.assert_awaited_once_with(
        temp_db,
        task=tasks.get_task(task.id),
        caller_session_id=caller.id,
        commit_sha=sha,
    )
