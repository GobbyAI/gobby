"""Coordinator claim release preserves a holder's unrelated task binding."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project

pytestmark = pytest.mark.unit


@dataclass
class ReleaseWorld:
    db: HubDatabase
    tasks: LocalTaskManager
    variables: SessionVariableManager
    registry: InternalToolRegistry
    coordinator: str
    holder: str
    receiver: str
    task: Task
    assigned: Task
    repo: Path

    async def release(self, caller: str | None = None) -> dict[str, Any]:
        with session_context_for_test(caller or self.coordinator):
            return cast(
                dict[str, Any],
                await self.registry.call(
                    "release_task_claim",
                    {
                        "task_id": self.task.id,
                        "holder_session_ref": self.holder,
                        "reason": "Move the parked task to its new lane",
                    },
                ),
            )


@pytest.fixture
def world(temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReleaseWorld:
    checkout = install_isolated_checkout_project(
        temp_db, tmp_path / "repo", name="claim-release", monkeypatch=monkeypatch
    )
    repo = Path(checkout.root_path)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)], check=True, capture_output=True
    )
    sessions = SessionManager(temp_db)
    coordinator, holder, receiver = (
        sessions.register(
            external_id=name,
            source="codex",
            machine_id=checkout.machine_id,
            project_id=checkout.project.id,
            workspace_path=str(repo),
        )
        for name in ("coordinator", "holder", "receiver")
    )
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        checkout.project.id,
        "Parked claim",
        claimed_by_session_id=holder.id,
        validation_criteria="The coordinator can release this claim safely.",
    )
    assigned = tasks.create_task(
        checkout.project.id,
        "Current work",
        claimed_by_session_id=holder.id,
        validation_criteria="An unrelated claim release preserves this task binding.",
    )
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        holder.id,
        {
            "claimed_tasks": {task.id: f"#{task.seq_num}", assigned.id: f"#{assigned.seq_num}"},
            "active_task_id": assigned.id,
            "assigned_task_id": assigned.id,
            "task_claimed": True,
            "step": "implement",
            "task_edited_files": {task.id: ["note.py"]},
            "task_edited_file_checkouts": {task.id: {str(repo): ["note.py"]}},
        },
    )
    AgentDefinitionManager(temp_db).create("orchestrator", {}, project_id=checkout.project.id)
    variables.set_variable(coordinator.id, "_agent_type", "orchestrator")
    return ReleaseWorld(
        temp_db,
        tasks,
        variables,
        create_task_registry(tasks),
        coordinator.id,
        holder.id,
        receiver.id,
        task,
        assigned,
        repo,
    )


@pytest.mark.asyncio
async def test_releases_parked_claim_and_allows_another_developer_to_claim(
    world: ReleaseWorld,
) -> None:
    result = await world.release()
    assert result.get("success") is True, result
    assert world.tasks.get_task(world.task.id).claimed_by_session_id is None
    with session_context_for_test(world.receiver):
        claimed = await world.registry.call("claim_task", {"task_id": world.task.id})
    assert claimed.get("success") is True, claimed
    assert world.tasks.get_task(world.task.id).claimed_by_session_id == world.receiver


@pytest.mark.asyncio
async def test_refuses_and_names_task_attributed_dirty_paths(world: ReleaseWorld) -> None:
    (world.repo / "note.py").write_text("in_flight = True\n")
    result = await world.release()
    assert result.get("error_code") == "task_has_uncommitted_changes", result
    assert "note.py" in str(result["paths"])
    assert world.tasks.get_task(world.task.id).claimed_by_session_id == world.holder


@pytest.mark.asyncio
async def test_refuses_non_coordinator(world: ReleaseWorld) -> None:
    result = await world.release(world.receiver)
    assert result.get("error_code") == "coordinator_required", result
    assert world.tasks.get_task(world.task.id).claimed_by_session_id == world.holder


@pytest.mark.asyncio
async def test_preserves_unrelated_assigned_task_and_edit_attribution(world: ReleaseWorld) -> None:
    before = world.variables.get_variables(world.holder)
    result = await world.release()
    assert result.get("success") is True, result
    after = world.variables.get_variables(world.holder)
    for key in ("assigned_task_id", "task_claimed", "step", "active_task_id", "task_edited_files"):
        assert after[key] == before[key], key
    assert world.task.id not in after["claimed_tasks"]
    assert world.assigned.id in after["claimed_tasks"]


@pytest.mark.asyncio
async def test_records_releasing_session_holder_and_reason(world: ReleaseWorld) -> None:
    result = await world.release()
    assert result.get("success") is True, result
    events = TaskLifecycleEventManager(world.db).list_lifecycle_events(world.task.id)
    event = events[-1]
    assert event.by_actor == world.coordinator
    assert world.holder in event.reason
    assert "Move the parked task to its new lane" in event.reason


def _lane_manager(world: ReleaseWorld, lane: str, holder_lane: str) -> None:
    sessions = SessionManager(world.db)
    caller = sessions.get(world.coordinator)
    assert caller is not None
    AgentDefinitionManager(world.db).create("lane-manager", {}, project_id=caller.project_id)
    world.variables.set_variable(caller.id, "_agent_type", "lane-manager")
    runs = LocalAgentRunManager(world.db)
    for session_id, agent, seat_lane in (
        (world.coordinator, "lane-manager", lane),
        (world.holder, "developer", holder_lane),
    ):
        run = runs.create(
            parent_session_id=world.receiver,
            provider="codex",
            prompt="Isolated seat fixture",
            agent_name=agent,
            child_session_id=session_id,
            resume_metadata_json={"lane": seat_lane},
        )
        runs.start(run.id)
        world.db.execute(
            "UPDATE sessions SET agent_run_id = %s WHERE id = %s", (run.id, session_id)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("lane,holder_lane,allowed", [("1", "1", True), ("2", "1", False)])
async def test_only_the_holders_lane_manager_can_release(
    world: ReleaseWorld,
    lane: str,
    holder_lane: str,
    allowed: bool,
) -> None:
    _lane_manager(world, lane, holder_lane)
    result = await world.release()
    if allowed:
        assert result.get("success") is True, result
        assert world.tasks.get_task(world.task.id).claimed_by_session_id is None
    else:
        assert result.get("error_code") == "coordinator_required", result
        assert world.tasks.get_task(world.task.id).claimed_by_session_id == world.holder


@pytest.mark.asyncio
async def test_lane_role_without_recorded_run_is_refused(world: ReleaseWorld) -> None:
    caller = SessionManager(world.db).get(world.coordinator)
    assert caller is not None
    AgentDefinitionManager(world.db).create("lane-manager", {}, project_id=caller.project_id)
    world.variables.set_variable(caller.id, "_agent_type", "lane-manager")
    result = await world.release()
    assert result.get("error_code") == "coordinator_required", result
    assert world.tasks.get_task(world.task.id).claimed_by_session_id == world.holder


@pytest.mark.asyncio
async def test_mismatched_holder_cannot_release_new_owners_claim(world: ReleaseWorld) -> None:
    world.tasks.claim_task(world.task.id, world.receiver, force=True)
    result = await world.release()
    assert result.get("error_code") == "task_claim_conflict", result
    assert world.tasks.get_task(world.task.id).claimed_by_session_id == world.receiver


@pytest.mark.asyncio
async def test_another_tasks_dirty_files_do_not_block_release(world: ReleaseWorld) -> None:
    (world.repo / "unrelated.py").write_text("in_flight = True\n")
    result = await world.release()
    assert result.get("success") is True, result
    assert (world.repo / "unrelated.py").read_text() == "in_flight = True\n"


@pytest.mark.asyncio
async def test_unavailable_attributed_checkout_fails_closed(world: ReleaseWorld) -> None:
    world.variables.merge_variables(
        world.holder,
        {
            "task_edited_file_checkouts": {
                world.task.id: {str(world.repo / "missing"): ["note.py"]}
            },
        },
    )
    result = await world.release()
    assert result.get("error_code") == "checkout_unresolved", result
    assert world.tasks.get_task(world.task.id).claimed_by_session_id == world.holder


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_ownership_and_projection(
    world: ReleaseWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = world.variables.get_variables(world.holder)

    def fail_audit(*args: Any, **kwargs: Any) -> None:
        raise ValueError("Audit storage unavailable")

    monkeypatch.setattr(TaskLifecycleEventManager, "record_lifecycle_event", fail_audit)
    result = await world.release()
    assert result.get("error") == "Audit storage unavailable", result
    assert world.tasks.get_task(world.task.id).claimed_by_session_id == world.holder
    after = world.variables.get_variables(world.holder)
    assert after["claimed_tasks"] == before["claimed_tasks"]
    assert after["active_task_id"] == before["active_task_id"]
