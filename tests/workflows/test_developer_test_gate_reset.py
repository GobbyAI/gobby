"""A claimed task never inherits the previous task's test gate.

Drives a synthetic developer-shaped workflow through two successive tasks on one seat.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.config.features import ToolResultOffloadConfig
from gobby.mcp_proxy.services.result_offload import ToolResultOffloader
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tool_results import ToolResultStore
from gobby.tasks.step_records import StepSubmission, record_task_step
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.workflows.test_seat_step_loops import (
    AUTHORITY_MACHINE_ID,
    PEER_ID,
    PROJECT_ID,
    SESSION_ID,
    SHA,
    SeatRun,
)

pytestmark = pytest.mark.unit

RESULT = "(tool_output.get('result') or tool_output)"


def _set(tool: str, variable: str, value: Any, when: str | None = None) -> dict[str, Any]:
    handler: dict[str, Any] = {
        "server": "gobby-tasks",
        "tool": tool,
        "action": "set_variable",
        "variable": variable,
        "value": value,
    }
    if when is not None:
        handler["when"] = when
    return handler


def _test_gate_workflow() -> dict[str, Any]:
    """Claim resets the test gate, an approved test record opens it, and close releases it."""
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/developer.yaml"
    )
    bundled = yaml.safe_load(path.read_text())["step_workflow"]
    await_tests = next(step for step in bundled["steps"] if step["name"] == "await_tests")
    return {
        "name": "inline-test-gate",
        "prompts": {"agent": "Exercise the task test gate."},
        "task_step_writes": ["implementation"],
        "workflows": {"rule_selectors": {"include": []}},
        "step_workflow": {
            "variables": {
                "required_skills": [],
                "assigned_task_id": None,
                "tests_ready": False,
                "implementation_complete": False,
            },
            "steps": [
                {
                    "name": "claim",
                    "on_mcp_success": [
                        _set("claim_task", "tests_ready", False),
                        _set("claim_task", "implementation_complete", False),
                        _set("claim_task", "assigned_task_id", f"{RESULT}.get('task_id')"),
                    ],
                    "transitions": [{"to": "await_tests", "when": "vars.assigned_task_id"}],
                },
                {
                    "name": "await_tests",
                    "allowed_tools": ["mcp__gobby__call_tool"],
                    "allowed_mcp_tools": ["gobby-tasks:get_task"],
                    "on_mcp_success": await_tests["on_mcp_success"],
                    "transitions": [{"to": "implement", "when": "vars.tests_ready"}],
                },
                {
                    "name": "implement",
                    "on_mcp_success": [_set("link_commit", "implementation_complete", True)],
                    "transitions": [{"to": "submit", "when": "vars.implementation_complete"}],
                },
                {
                    "name": "submit",
                    "on_mcp_success": [
                        _set(
                            "get_task",
                            "assigned_task_id",
                            None,
                            when=f"{RESULT}.get('state', {{}}).get('is_closed') is True",
                        )
                    ],
                    "transitions": [{"to": "claim", "when": "not vars.assigned_task_id"}],
                },
            ],
        },
    }


@pytest.fixture
def seat(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> SeatRun:
    patch_local_machine_id(monkeypatch, AUTHORITY_MACHINE_ID)
    run = SeatRun(temp_db, "inline-test-gate", definition=_test_gate_workflow())
    AgentDefinitionManager(temp_db).create(
        "inline-test-author",
        {
            "name": "inline-test-author",
            "prompts": {"agent": "Record test evidence."},
            "task_step_writes": ["test"],
            "workflows": {"rule_selectors": {"include": []}},
        },
        project_id=PROJECT_ID,
    )
    SessionVariableManager(temp_db).set_variable(PEER_ID, "_agent_type", "inline-test-author")
    return run


async def _claim(run: SeatRun, task: Task) -> None:
    await run.call(
        "gobby-tasks",
        "claim_task",
        {"task_id": task.id},
        {"result": {"task_id": task.id, "ref": f"#{task.seq_num}"}},
    )


async def _refresh(run: SeatRun, task: Task) -> None:
    with session_context_for_test(SESSION_ID):
        output = await create_task_registry(LocalTaskManager(run.db)).call(
            "get_task", {"task_id": task.id, "brief": False}
        )
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, output)


def _approve_committed_tests(run: SeatRun, task: Task) -> None:
    LocalTaskManager(run.db).link_commit(task.id, SHA)
    record_task_step(
        run.db,
        task_id=task.id,
        session_id=PEER_ID,
        step="test",
        submission=StepSubmission(
            status="approved",
            epoch=task.review_epoch,
            round_number=1,
            feedback="Committed failing acceptance tests; expected assertion failures inspected.",
            commit_sha=SHA,
        ),
    )


def _approve_moot_tests(run: SeatRun, task: Task) -> None:
    record_task_step(
        run.db,
        task_id=task.id,
        session_id=PEER_ID,
        step="test",
        submission=StepSubmission(
            status="approved",
            epoch=task.review_epoch,
            round_number=1,
            feedback="Existing tests cover every criterion; no new tests needed.",
            disposition="moot",
        ),
    )


async def _implement_and_close(run: SeatRun, task: Task) -> None:
    """Drive one approved task through its candidate and the Dev Manager's close."""
    await _refresh(run, task)
    assert run.step == "implement"
    await run.call(
        "gobby-tasks",
        "link_commit",
        {"task_id": task.id, "commit_sha": SHA},
        {"result": {"production_changes": True}},
    )
    assert run.step == "submit"
    await run.send(f"EVENT=CANDIDATE TASK=#{task.seq_num} RUN=run WT=tree COMMIT={SHA} NOTE=ready")
    await run.call(
        "gobby-tasks",
        "get_task",
        {"task_id": task.id},
        {"result": {"id": task.id, "ref": f"#{task.seq_num}", "state": {"is_closed": True}}},
    )
    assert run.step == "claim"


@pytest.mark.asyncio
async def test_second_claim_waits_for_its_own_test_record(seat: SeatRun) -> None:
    await seat.load(*seat.vars["required_skills"])
    assert seat.step == "claim"

    first = seat.task(owner=SESSION_ID)
    await _claim(seat, first)
    await _refresh(seat, first)
    assert seat.step == "await_tests"
    _approve_committed_tests(seat, first)
    await _refresh(seat, first)
    await _implement_and_close(seat, first)

    second = seat.task(owner=SESSION_ID)
    await _claim(seat, second)
    await _refresh(seat, second)
    assert seat.step == "await_tests"
    assert seat.vars["tests_ready"] is False
    assert not await seat.admits_native("Edit", {"file_path": "/repo/src/feature.py"})
    assert not await seat.admits_native("Bash", {"command": "touch src/feature.py"})

    _approve_moot_tests(seat, second)
    await _refresh(seat, second)
    assert seat.step == "implement"


async def _get_task_as_documented(run: SeatRun, task: Task, *, brief: bool) -> None:
    """Call get_task the way the await step documents it: by assigned_task_id, brief set."""
    arguments: dict[str, Any] = {"task_id": task.id, "brief": brief}
    with session_context_for_test(SESSION_ID):
        output = await create_task_registry(LocalTaskManager(run.db)).call("get_task", arguments)
    await run.call("gobby-tasks", "get_task", arguments, output)


@pytest.mark.asyncio
async def test_documented_get_task_call_opens_the_gate(seat: SeatRun) -> None:
    await seat.load(*seat.vars["required_skills"])
    task = seat.task(owner=SESSION_ID)
    await _claim(seat, task)
    _approve_committed_tests(seat, task)
    await _get_task_as_documented(seat, task, brief=True)
    assert seat.step == "await_tests"
    await _get_task_as_documented(seat, task, brief=False)
    assert seat.step == "implement"


async def _offloaded_task_card(run: SeatRun, task: Task) -> dict[str, Any]:
    """Return the model-facing result of a real oversized get_task call."""
    LocalTaskManager(run.db).update_task(task.id, description="Long task evidence. " * 1_000)
    with session_context_for_test(SESSION_ID):
        output = await create_task_registry(LocalTaskManager(run.db)).call(
            "get_task", {"task_id": task.id, "brief": False}
        )
    config = ToolResultOffloadConfig()
    assert len(json.dumps(output, ensure_ascii=False, default=str)) > config.threshold_chars
    offloader = ToolResultOffloader(
        ToolResultStore(run.db, config), run.db, config, lambda: PROJECT_ID
    )
    envelope = await offloader.maybe_offload(
        server_name="gobby-tasks",
        tool_name="get_task",
        result=output,
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        intent=None,
    )
    assert isinstance(envelope, dict)
    assert envelope["offloaded"] is True
    assert envelope["retrieval_available"] is True
    assert len(json.dumps(envelope, ensure_ascii=False, default=str)) <= config.max_envelope_chars
    return envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", ["current", "stale", "missing"])
async def test_oversized_get_task_preserves_test_gate(seat: SeatRun, approval: str) -> None:
    task = seat.task(owner=SESSION_ID)
    await _claim(seat, task)
    if approval != "missing":
        _approve_committed_tests(seat, task)
    if approval == "stale":
        seat.db.execute(
            "UPDATE tasks SET review_epoch = review_epoch + 1 WHERE id = %s", (task.id,)
        )

    envelope = await _offloaded_task_card(seat, task)
    await seat.call(
        "gobby-tasks",
        "get_task",
        {"task_id": task.id, "brief": False},
        {"success": True, "result": envelope},
    )

    assert seat.step == ("implement" if approval == "current" else "await_tests")
    assert seat.vars["tests_ready"] is (approval == "current")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent,source,target",
    [
        ("developer", "route_skills", "load_additional_skills"),
        ("developer", "implement", "claim"),
        ("developer", "submit", "claim"),
        ("epic-reviewer", "claim", "load_skill"),
        ("tech-writer", "implement", "terminate"),
    ],
)
@pytest.mark.parametrize("oversized", [False, True], ids=["inline", "offloaded"])
async def test_get_task_field_transitions_survive_offload(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    source: str,
    target: str,
    oversized: bool,
) -> None:
    """Exercise bundled identity/closed-card consumers using their real YAML handlers."""
    patch_local_machine_id(monkeypatch, AUTHORITY_MACHINE_ID)
    path = (
        Path(__file__).resolve().parents[2]
        / f"src/gobby/install/shared/workflows/agents/{agent}.yaml"
    )
    bundled = yaml.safe_load(path.read_text())["step_workflow"]
    source_step = next(step for step in bundled["steps"] if step["name"] == source)
    handlers = [
        handler
        for handler in source_step["on_mcp_success"]
        if handler.get("server") == "gobby-tasks" and handler.get("tool") == "get_task"
    ]
    assert handlers
    workflow = _test_gate_workflow()
    workflow["step_workflow"] = {
        "variables": {**bundled["variables"], "task_claimed": agent == "developer"},
        "steps": [
            {
                "name": source,
                "allowed_tools": "all",
                "on_mcp_success": handlers,
                "transitions": [t for t in source_step["transitions"] if t["to"] == target],
            },
            {"name": target, "allowed_tools": "all"},
        ],
    }
    run = SeatRun(temp_db, "inline-test-gate", definition=workflow)
    task = run.task(owner=SESSION_ID)
    instance = run.instances.get_for_session(SESSION_ID)
    assert instance is not None
    instance.variables.update(assigned_task_id=task.id, assigned_task_uuid=task.id)
    run.instances.save(instance)
    if source != "route_skills":
        # Seed the observed state; testing close gates requires an unrelated checkout.
        temp_db.execute(
            "UPDATE tasks SET closed_at = CURRENT_TIMESTAMP, closed_reason = 'done', "
            "claimed_by_session_id = NULL WHERE id = %s",
            (task.id,),
        )
    if oversized:
        output = await _offloaded_task_card(run, task)
    else:
        with session_context_for_test(SESSION_ID):
            output = await create_task_registry(LocalTaskManager(temp_db)).call(
                "get_task", {"task_id": task.id, "brief": False}
            )

    await run.call(
        "gobby-tasks",
        "get_task",
        {"task_id": task.id, "brief": False},
        {"success": True, "result": output},
    )

    assert run.step == target
