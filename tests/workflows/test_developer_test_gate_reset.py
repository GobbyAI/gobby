"""A claimed task never inherits the previous task's test gate.

Drives a synthetic developer-shaped workflow through two successive tasks on one seat.
"""

from __future__ import annotations

from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
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
    test_record = f"{RESULT}.get('step_records', {{}}).get('test', {{}})"
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
                    "on_mcp_success": [
                        _set(
                            "get_task",
                            "tests_ready",
                            f"{test_record}.get('status') == 'approved' and "
                            f"{test_record}.get('epoch') == {RESULT}.get('review_epoch')",
                            when=f"{RESULT}.get('id') == vars.assigned_task_id",
                        )
                    ],
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
