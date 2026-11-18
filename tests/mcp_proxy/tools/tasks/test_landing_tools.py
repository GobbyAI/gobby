"""set_landing_freeze stores the freeze in the project's git common dir."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.code_index.context import CodeIndexContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._ops_factory import create_task_ops_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.landing_policy import freeze_path, read_freeze, write_freeze
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit

UNKNOWN_TASK_UUID = "00000000-0000-4000-8000-000000000001"


@dataclass(frozen=True)
class _FreezeWorld:
    registry: InternalToolRegistry
    db: HubDatabase
    common_dir: Path
    project_name: str
    holder: str
    other: str
    task: Task


@pytest.fixture
def freeze_world(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _FreezeWorld:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", monkeypatch=monkeypatch
    )
    subprocess.run(["git", "init", "-q", isolated.root_path], check=True)
    sessions = SessionManager(temp_db)
    holder, other = [
        sessions.register(
            external_id=external_id,
            machine_id=isolated.machine_id,
            source="codex",
            project_id=isolated.project.id,
        )
        for external_id in ("freeze-holder", "freeze-other")
    ]
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        isolated.project.id,
        "Bound candidate",
        created_in_session_id=holder.id,
        claimed_by_session_id=holder.id,
        validation_criteria="Land the bound candidate.",
    )
    return _FreezeWorld(
        registry=create_task_ops_registry(tasks),
        db=temp_db,
        common_dir=Path(isolated.root_path) / ".git",
        project_name=isolated.project.name,
        holder=holder.id,
        other=other.id,
        task=task,
    )


async def _set_freeze(
    world: _FreezeWorld, session_id: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    with session_context_for_test(session_id):
        outcome: dict[str, Any] = await world.registry.call("set_landing_freeze", arguments)
        return outcome


def _task_ref(world: _FreezeWorld, form: str) -> str:
    refs = {
        "uuid": world.task.id,
        "number": f"#{world.task.seq_num}",
        "project": f"{world.project_name}#{world.task.seq_num}",
    }
    return refs[form]


def _bind_freeze(world: _FreezeWorld) -> None:
    write_freeze(
        world.common_dir,
        on=True,
        reason="release hold",
        session_id=world.holder,
        task_id=world.task.id,
    )


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
    code_index = MagicMock(spec=CodeIndexContext)
    registry = create_task_ops_registry(tasks, code_index=code_index)
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
        code_index=code_index,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["uuid", "number", "project"])
async def test_claim_holder_binds_the_freeze_to_its_task(
    freeze_world: _FreezeWorld, form: str
) -> None:
    result = await _set_freeze(
        freeze_world,
        freeze_world.holder,
        {"on": True, "reason": "release hold", "task_id": _task_ref(freeze_world, form)},
    )

    stored = read_freeze(freeze_world.common_dir)
    assert result["success"] is True
    assert result["freeze"]["task_id"] == freeze_world.task.id
    assert (stored.on, stored.task_id) == (True, freeze_world.task.id)


@pytest.mark.asyncio
async def test_bind_requires_the_caller_to_hold_the_task_claim(
    freeze_world: _FreezeWorld,
) -> None:
    write_freeze(
        freeze_world.common_dir,
        on=True,
        reason="release hold",
        session_id=freeze_world.holder,
    )

    result = await _set_freeze(
        freeze_world,
        freeze_world.other,
        {"on": True, "reason": "other hold", "task_id": freeze_world.task.id},
    )

    stored = read_freeze(freeze_world.common_dir)
    assert result["success"] is False
    assert result["error_code"] == "landing_freeze_claim_required"
    assert (stored.reason, stored.task_id) == ("release hold", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("task_ref", ["#999999", UNKNOWN_TASK_UUID])
async def test_bind_to_an_unknown_task_is_refused_and_leaves_the_freeze_off(
    freeze_world: _FreezeWorld, task_ref: str
) -> None:
    result = await _set_freeze(
        freeze_world,
        freeze_world.holder,
        {"on": True, "reason": "release hold", "task_id": task_ref},
    )

    assert result["success"] is False
    assert read_freeze(freeze_world.common_dir).on is False


@pytest.mark.asyncio
async def test_clearing_with_a_task_id_is_refused(freeze_world: _FreezeWorld) -> None:
    write_freeze(
        freeze_world.common_dir,
        on=True,
        reason="release hold",
        session_id=freeze_world.holder,
    )

    result = await _set_freeze(
        freeze_world,
        freeze_world.holder,
        {"on": False, "reason": "shipped", "task_id": freeze_world.task.id},
    )

    stored = read_freeze(freeze_world.common_dir)
    assert result["success"] is False
    assert "task_id" in result["error"]
    assert (stored.on, stored.reason, stored.task_id) == (True, "release hold", None)


@pytest.mark.asyncio
async def test_bound_freeze_refuses_a_set_from_another_session(
    freeze_world: _FreezeWorld,
) -> None:
    _bind_freeze(freeze_world)

    result = await _set_freeze(
        freeze_world, freeze_world.other, {"on": True, "reason": "other hold"}
    )

    stored = read_freeze(freeze_world.common_dir)
    assert result["success"] is False
    assert result["error_code"] == "landing_freeze_bound"
    assert result["task_id"] == freeze_world.task.id
    assert (stored.reason, stored.task_id) == ("release hold", freeze_world.task.id)


@pytest.mark.asyncio
async def test_bound_freeze_refuses_a_clear_from_another_session(
    freeze_world: _FreezeWorld,
) -> None:
    _bind_freeze(freeze_world)

    result = await _set_freeze(freeze_world, freeze_world.other, {"on": False, "reason": "shipped"})

    stored = read_freeze(freeze_world.common_dir)
    assert result["success"] is False
    assert result["error_code"] == "landing_freeze_bound"
    assert result["task_id"] == freeze_world.task.id
    assert (stored.on, stored.task_id) == (True, freeze_world.task.id)


@pytest.mark.asyncio
async def test_claim_holder_may_clear_a_bound_freeze(freeze_world: _FreezeWorld) -> None:
    _bind_freeze(freeze_world)

    result = await _set_freeze(
        freeze_world, freeze_world.holder, {"on": False, "reason": "shipped"}
    )

    stored = read_freeze(freeze_world.common_dir)
    assert result["success"] is True
    assert (stored.on, stored.reason) == (False, "shipped")


@pytest.mark.asyncio
async def test_claim_holder_may_set_a_bound_freeze_again(freeze_world: _FreezeWorld) -> None:
    _bind_freeze(freeze_world)

    result = await _set_freeze(
        freeze_world,
        freeze_world.holder,
        {"on": True, "reason": "still holding", "task_id": freeze_world.task.id},
    )

    stored = read_freeze(freeze_world.common_dir)
    assert result["success"] is True
    assert (stored.on, stored.reason, stored.task_id) == (
        True,
        "still holding",
        freeze_world.task.id,
    )


@pytest.mark.asyncio
async def test_bound_freeze_refuses_its_former_holder_after_the_claim_is_released(
    freeze_world: _FreezeWorld,
) -> None:
    _bind_freeze(freeze_world)
    freeze_world.db.execute(
        "UPDATE tasks SET claimed_by_session_id = NULL WHERE id = %s", (freeze_world.task.id,)
    )

    result = await _set_freeze(
        freeze_world, freeze_world.holder, {"on": False, "reason": "shipped"}
    )

    assert result["success"] is False
    assert result["error_code"] == "landing_freeze_bound"
    assert read_freeze(freeze_world.common_dir).on is True


@pytest.mark.asyncio
async def test_unbound_freeze_is_set_by_another_session_with_no_bound_task(
    freeze_world: _FreezeWorld,
) -> None:
    result = await _set_freeze(
        freeze_world, freeze_world.other, {"on": True, "reason": "release hold"}
    )

    assert result["success"] is True
    assert result["freeze"]["task_id"] is None
    assert read_freeze(freeze_world.common_dir).on is True


@pytest.mark.asyncio
async def test_unreadable_freeze_is_cleared_by_another_session(
    freeze_world: _FreezeWorld,
) -> None:
    marker = freeze_path(freeze_world.common_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{not json", encoding="utf-8")

    result = await _set_freeze(freeze_world, freeze_world.other, {"on": False, "reason": "shipped"})

    assert result["success"] is True
    assert result["freeze"]["task_id"] is None
    assert read_freeze(freeze_world.common_dir).on is False
