"""Resumed seats bind claims through definition-driven workflow handlers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.hooks.events import HookEventType
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import Task
from tests.fixtures.agent_definitions import synthetic_developer_body
from tests.workflows.test_seat_step_loops import COORDINATOR_ID, SESSION_ID, SeatRun

pytestmark = pytest.mark.unit


def _claim_definition() -> dict[str, Any]:
    definition = synthetic_developer_body().model_dump(mode="json")
    workflow = definition["step_workflow"]
    bootstrap, claim = workflow["steps"][:2]
    for step in (bootstrap, claim):
        step["allowed_tools"] = ["Read", "mcp__gobby__call_tool"]
    bootstrap["allowed_mcp_tools"] = ["gobby-skills:get_skill"]
    bootstrap["transitions"] = [
        {"to": "claim", "when": "all(skill_loaded(skill) for skill in vars.required_skills)"}
    ]
    claim["allowed_mcp_tools"] = ["gobby-tasks:claim_task"]
    claim["on_mcp_success"].extend(
        {
            "server": "gobby-tasks",
            "tool": "claim_task",
            "action": "set_variable",
            "variable": variable,
            "value": value,
        }
        for variable, value in (
            ("task_claimed", True),
            ("assigned_task_ref", "(tool_output.get('result') or tool_output).get('ref')"),
        )
    )
    claim["transitions"] = [{"to": "route_skills", "when": "vars.task_claimed"}]
    return definition


async def _developer_at_claim(temp_db: HubDatabase) -> tuple[SeatRun, Task]:
    run = SeatRun(temp_db, "developer", definition=_claim_definition())
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


async def test_bootstrap_admits_only_active_runs_owned_claim_and_binds_after_skills(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = SeatRun(temp_db, "developer", definition=_claim_definition())
    task = run.task(owner=SESSION_ID)
    foreign_task = run.task()
    assert not await run.admits("gobby-tasks", "claim_task", {"task_id": task.id})
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

    assert await run.admits("gobby-tasks", "claim_task", {"task_id": task.id})
    assert not await run.admits("gobby-tasks", "claim_task", {"task_id": foreign_task.id})
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
