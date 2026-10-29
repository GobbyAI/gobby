"""Resumed developer seats bind claims through the bundled workflow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gobby.hooks.events import HookEventType
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import Task
from tests.workflows.test_seat_step_loops import COORDINATOR_ID, SESSION_ID, SeatRun

pytestmark = pytest.mark.unit

_BUNDLED_DEVELOPER = (
    Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/agents/developer.yaml"
)


def test_bundled_developer_has_no_pre_v4_land_handoff_loop() -> None:
    """The Dev Manager owns landing/close; the developer retains its claim until then."""
    definition = yaml.safe_load(_BUNDLED_DEVELOPER.read_text())
    workflow = definition["step_workflow"]
    assert "_developer_handoff_tasks" not in workflow["variables"]
    for step in workflow["steps"]:
        assert "Handed off: reviewer LAND" not in step.get("status_message", "")
        assert not [
            handler
            for handler in step.get("on_mcp_success", [])
            if handler.get("server") == "gobby-tasks" and handler.get("tool") == "escalate_task"
        ]


async def _developer_at_claim(temp_db: HubDatabase) -> tuple[SeatRun, Task]:
    definition = yaml.safe_load(_BUNDLED_DEVELOPER.read_text())
    run = SeatRun(temp_db, "developer", definition=definition)
    await run.load(*run.vars["required_skills"])
    assert run.step == "claim"
    return run, run.task(owner=SESSION_ID)


def _already_claimed(task: Task) -> dict[str, Any]:
    ref = f"#{task.seq_num}"
    return {
        "success": True,
        "task_id": task.id,
        "ref": ref,
        "title": task.title,
        "already_claimed": True,
        "message": f"Task {ref} is selected. Use claim_task to select another owned task.",
    }


async def test_resumed_seat_with_restored_selection_binds_on_already_claimed(
    temp_db: HubDatabase,
) -> None:
    run, task = await _developer_at_claim(temp_db)
    run.session_vars.update(
        task_claimed=True,
        active_task_id=task.id,
        claimed_tasks={task.id: f"#{task.seq_num}"},
    )

    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": f"#{task.seq_num}"},
        {"success": True, "result": _already_claimed(task)},
    )

    assert run.step == "route_skills"
    assert run.vars["task_claimed"] is True
    assert run.vars["assigned_task_id"] == task.id
    assert run.vars["assigned_task_ref"] == f"#{task.seq_num}"


async def test_claim_during_bootstrap_binds_without_repeating_claim(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = yaml.safe_load(_BUNDLED_DEVELOPER.read_text())
    run = SeatRun(temp_db, "developer", definition=definition)
    task = run.task(owner=SESSION_ID)
    runs = LocalAgentRunManager(temp_db)
    runs.create(
        parent_session_id=COORDINATOR_ID,
        provider="claude",
        prompt="Resume the claimed task",
        agent_name="developer",
        child_session_id=SESSION_ID,
        task_id=task.id,
    )
    monkeypatch.setattr(run.engine, "_runner", SimpleNamespace(run_storage=runs))
    run.session_vars.update(
        task_claimed=True,
        active_task_id=task.id,
        claimed_tasks={task.id: f"#{task.seq_num}"},
    )

    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": f"#{task.seq_num}"},
        {"success": True, "result": _already_claimed(task)},
    )
    assert run.step == "load_skills"
    assert not await run.admits_native("Edit", {"file_path": "src/example.py"})
    await run.load(*run.vars["required_skills"])

    assert run.step == "route_skills"
    assert run.vars["task_claimed"] is True
    assert run.vars["assigned_task_id"] == task.id
    assert run.vars["assigned_task_ref"] == f"#{task.seq_num}"


async def test_claim_step_denial_names_claim_task(temp_db: HubDatabase) -> None:
    run, _ = await _developer_at_claim(temp_db)
    event = run._event(
        HookEventType.BEFORE_TOOL,
        {"tool_name": "Edit", "tool_input": {"file_path": "src/example.py"}},
    )
    response = await run.engine.evaluate(event, session_id=SESSION_ID, variables=run.session_vars)

    assert response.decision == "block"
    assert "claim_task" in (response.reason or "")
