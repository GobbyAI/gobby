"""Synthetic workflow prerequisites and observable out-of-order commit incidents.

Bundled YAML wiring and recovery prose are reviewed separately.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.agents.team_admission_records import TeamBinding
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.task_commits import create_commit_registry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.step_records import StepSubmission, record_task_step
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_state import ExecutionStatus
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.workflows.test_seat_step_loops import (
    AUTHORITY_MACHINE_ID,
    COORDINATOR_ID,
    OWNER_ID,
    PEER_ID,
    PROJECT_ID,
    SESSION_ID,
    SHA,
    SeatRun,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def builder(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> SeatRun:
    """Exercise existing declarative handlers against the public task projection."""
    patch_local_machine_id(monkeypatch, AUTHORITY_MACHINE_ID)
    record = "(tool_output.get('result') or tool_output).get('step_records', {}).get('test', {})"
    run = SeatRun(
        temp_db,
        "inline-builder",
        definition={
            "name": "inline-builder",
            "task_step_writes": ["implementation"],
            "prompts": {"agent": "Implement after tests are recorded."},
            "workflows": {"rule_selectors": {"include": []}},
            "step_workflow": {
                "variables": {"assigned_task_id": None, "tests_ready": False},
                "steps": [
                    {
                        "name": "await_tests",
                        "allowed_tools": ["Read", "mcp__gobby__call_tool"],
                        "allowed_mcp_tools": ["gobby-tasks:get_task"],
                        "on_mcp_success": [
                            {
                                "server": "gobby-tasks",
                                "tool": "get_task",
                                "action": "set_variable",
                                "variable": "tests_ready",
                                "value": (
                                    f"{record}.get('status') == 'approved' and "
                                    f"{record}.get('epoch') == "
                                    "(tool_output.get('result') or tool_output).get('review_epoch')"
                                ),
                                "when": "tool_input.get('task_id') == vars.assigned_task_id",
                            }
                        ],
                        "transitions": [{"to": "implement", "when": "vars.tests_ready"}],
                    },
                    {"name": "implement", "allowed_tools": "all"},
                ],
            },
        },
    )
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


@pytest.fixture
def unit(builder: SeatRun) -> Task:
    task = builder.task(owner=SESSION_ID)
    instance = builder.instances.get_for_session(SESSION_ID)
    assert instance is not None
    instance.variables["assigned_task_id"] = task.id
    builder.instances.save(instance)
    return task


async def _refresh(run: SeatRun, task: Task) -> None:
    with session_context_for_test(SESSION_ID):
        output = await create_task_registry(LocalTaskManager(run.db)).call(
            "get_task", {"task_id": task.id, "brief": False}
        )
    await run.call("gobby-tasks", "get_task", {"task_id": task.id}, output)


def _test_record(run: SeatRun, task: Task, *, committed: bool = False) -> None:
    if committed:
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
            feedback=(
                "Committed failing acceptance tests; expected assertion failures inspected."
                if committed
                else "Existing tests cover every criterion; no new tests needed."
            ),
            commit_sha=SHA if committed else None,
            disposition=None if committed else "moot",
        ),
    )


@pytest.mark.asyncio
async def test_missing_test_step_keeps_implementation_and_writes_closed(
    builder: SeatRun,
    unit: Task,
) -> None:
    await _refresh(builder, unit)
    assert builder.step == "await_tests"
    assert not await builder.admits_native("Edit", {"file_path": "/repo/src/feature.py"})
    assert not await builder.admits_native("Bash", {"command": "touch src/feature.py"})


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [True, False], ids=["committed-tests", "no-new-tests"])
async def test_completed_test_step_opens_implementation(
    builder: SeatRun,
    unit: Task,
    committed: bool,
) -> None:
    _test_record(builder, unit, committed=committed)
    await _refresh(builder, unit)
    assert builder.step == "implement"
    assert await builder.admits_native("Edit", {"file_path": "/repo/src/feature.py"})


@pytest.mark.asyncio
async def test_other_tasks_test_record_does_not_open_wait(builder: SeatRun, unit: Task) -> None:
    other = builder.task(owner=SESSION_ID)
    _test_record(builder, other)
    await _refresh(builder, other)
    await _refresh(builder, unit)
    assert builder.step == "await_tests"
    assert builder.vars["tests_ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "in_progress", "rejected"])
async def test_unfinished_or_rejected_test_step_keeps_wait_closed(
    builder: SeatRun, unit: Task, status: str
) -> None:
    record_task_step(
        builder.db,
        task_id=unit.id,
        session_id=PEER_ID,
        step="test",
        submission=StepSubmission.model_validate(
            {
                "status": status,
                "epoch": unit.review_epoch,
                "round_number": 1,
                "feedback": "Test preparation has not completed successfully.",
            }
        ),
    )
    await _refresh(builder, unit)
    assert builder.step == "await_tests"


@pytest.mark.asyncio
async def test_previous_epoch_test_record_does_not_open_wait(builder: SeatRun, unit: Task) -> None:
    _test_record(builder, unit)
    builder.db.execute("UPDATE tasks SET review_epoch = review_epoch + 1 WHERE id = %s", (unit.id,))
    await _refresh(builder, unit)
    assert builder.step == "await_tests"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["", "   "], ids=["empty", "whitespace"])
async def test_no_new_tests_requires_a_reason(builder: SeatRun, unit: Task, reason: str) -> None:
    with session_context_for_test(PEER_ID):
        result = await create_task_registry(LocalTaskManager(builder.db)).call(
            "record_test_step",
            {
                "task_id": unit.id,
                "status": "approved",
                "epoch": unit.review_epoch,
                "round_number": 1,
                "disposition": "moot",
                "feedback": reason,
            },
        )
    assert "feedback" in result["error"] or "at least 1 character" in result["error"]
    await _refresh(builder, unit)
    assert builder.step == "await_tests"


@pytest.fixture
def links(builder: SeatRun, unit: Task, monkeypatch: pytest.MonkeyPatch) -> InternalToolRegistry:
    """Stub Git boundaries; keep storage, caller authority and messaging real."""
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.task_commits.resolve_task_repo_path", lambda **kwargs: "/repo"
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.task_commits.normalize_commit_sha", AsyncMock(return_value=SHA)
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._task_scope.collect_net_commit_paths_async",
        AsyncMock(return_value=SimpleNamespace(changed={"src/feature.py"}, deleted=set())),
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.task_commits.assert_task_commit_paths_available_async", AsyncMock()
    )
    runs = LocalAgentRunManager(builder.db)
    run = runs.create(
        parent_session_id=COORDINATOR_ID,
        provider="codex",
        prompt="Implement the assigned unit",
        agent_name="inline-builder",
        child_session_id=SESSION_ID,
        task_id=unit.id,
        machine_id=AUTHORITY_MACHINE_ID,
    )
    runs.start(run.id)
    builder.db.execute("UPDATE sessions SET agent_run_id = %s WHERE id = %s", (run.id, SESSION_ID))
    return create_commit_registry(LocalTaskManager(builder.db), session_manager=builder.sessions)


def _team(run: SeatRun, task: Task, *, mode: str = "single") -> None:
    executions = LocalPipelineExecutionManager(run.db, PROJECT_ID)
    definition = PipelineDefinition(
        name="inline-team", steps=[PipelineStep(id="admit", exec="true")]
    )
    execution = executions.create_execution(
        definition.name,
        json.dumps({"task": task.id, "report_to": OWNER_ID}),
        definition_json=definition.model_dump_json(),
        project_id=PROJECT_ID,
    )
    step = executions.create_step_execution(execution.id, "admit")
    binding = TeamBinding(
        team_id=execution.id,
        task_id=task.id,
        project_id=PROJECT_ID,
        branch="inline-task",
        base_branch="integration",
        worktree_id=None,
        worktree_path=None,
        mode=mode,
    )
    executions.update_step_execution(step.id, output_json=json.dumps({"team": asdict(binding)}))
    session = run.sessions.register(
        external_id=f"pipeline-{execution.id}",
        machine_id=AUTHORITY_MACHINE_ID,
        source="pipeline",
        project_id=PROJECT_ID,
        parent_session_id=COORDINATOR_ID,
    )
    executions.update_execution_session(execution.id, session.id)
    executions.update_execution_status(execution.id, ExecutionStatus.RUNNING)
    run.db.execute(
        "UPDATE sessions SET parent_session_id = %s WHERE id = %s", (session.id, SESSION_ID)
    )


async def _link(links: InternalToolRegistry, task: Task) -> dict[str, Any]:
    with session_context_for_test(SESSION_ID):
        result = await links.call("link_commit", {"task_id": task.id, "commit_sha": SHA})
    return cast(dict[str, Any], result)


@pytest.mark.asyncio
async def test_out_of_order_link_records_task_seat_session_commit_and_failed_gate(
    builder: SeatRun,
    unit: Task,
    links: InternalToolRegistry,
) -> None:
    await _link(links, unit)
    rows = builder.db.fetchall(
        "SELECT task_id, author, body FROM task_comments WHERE task_id = %s", (unit.id,)
    )
    evidence = "\n".join(str(dict(row)) for row in rows)
    assert evidence, "Implementation linked before the test step must leave durable task evidence"
    for value in (unit.id, SESSION_ID, "inline-builder", SHA, "test"):
        assert value in evidence
    assert any(
        word in evidence.lower() for word in ("before", "incomplete", "missing", "out_of_order")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True], ids=["parent-only", "team-report-to"])
async def test_out_of_order_link_notifies_existing_parent_and_team_report_to(
    builder: SeatRun,
    unit: Task,
    links: InternalToolRegistry,
    team: bool,
) -> None:
    if team:
        _team(builder, unit)
    await _link(links, unit)
    messages = InterSessionMessageManager(builder.db)
    for recipient in [COORDINATOR_ID, OWNER_ID] if team else [COORDINATOR_ID]:
        delivered = messages.get_messages(recipient)
        assert delivered, f"Missing out-of-order implementation alert to {recipient}"
        evidence = "\n".join(message.content for message in delivered)
        assert unit.id in evidence
        assert SHA in evidence
        assert SESSION_ID in evidence
    if not team:
        assert messages.get_messages(OWNER_ID) == []


@pytest.mark.asyncio
async def test_ordered_implementation_link_does_not_raise_an_incident(
    builder: SeatRun,
    unit: Task,
    links: InternalToolRegistry,
) -> None:
    _test_record(builder, unit)
    result = await _link(links, unit)
    assert "error" not in result
    assert SHA in (LocalTaskManager(builder.db).get_task(unit.id).commits or [])
    assert InterSessionMessageManager(builder.db).get_messages(COORDINATOR_ID) == []
