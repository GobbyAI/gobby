"""Bundled `planning` runbook: sync, seat launches, restart, resume refusal."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
    RUNBOOK_RESUME_REFUSED,
    _background_tasks_by_execution,
    resume_interrupted_pipelines,
    resume_pipeline,
    run_pipeline,
)
from gobby.servers.routes.pipelines import create_pipelines_router
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager, system_session_id
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline_executor import PipelineExecutor, step_invocation_id
from gobby.workflows.pipeline_loader import PipelineLoader
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus
from gobby.workflows.sync_pipelines import sync_bundled_pipelines
from gobby.workflows.templates import TemplateEngine

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


class RunbookAgentsProxy:
    """Stub gobby-agents proxy: records each call and answers spawns like a placed reply."""

    def __init__(self, fail_agents: frozenset[str] = frozenset()) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_agents = fail_agents

    async def get_tool_schema(
        self, server: str, tool: str, *, session_id: str | None
    ) -> dict[str, Any]:
        return {"success": True, "tool": {"inputSchema": {}}}

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
        enforce_workflow: bool,
    ) -> dict[str, Any]:
        project = get_project_context() or {}
        self.calls.append(
            {
                "tool": f"{server}:{tool}",
                "arguments": arguments,
                "session_id": session_id,
                "ambient_session_id": get_current_session_id(),
                "ambient_project_id": project.get("id"),
            }
        )
        if tool == "check_runbook_seats":
            return {"success": True}
        if arguments["agent"] in self.fail_agents:
            return {"success": False, "error": "spawn refused"}
        index = len(self.calls)
        return {"success": True, "run_id": f"run-{index}", "pane_ref": f"pane-{index}"}

    def spawns(self) -> list[dict[str, Any]]:
        return [call["arguments"] for call in self.calls if call["tool"].endswith("spawn_agent")]


def _executor(temp_db: HubDatabase, proxy: RunbookAgentsProxy) -> PipelineExecutor:
    return PipelineExecutor(
        db=temp_db,
        execution_manager=LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID),
        llm_service=MagicMock(),
        loader=PipelineLoader(temp_db),
        tool_proxy_getter=lambda: proxy,
        template_engine=TemplateEngine(),
        session_manager=SessionManager(temp_db),
    )


async def _planning(temp_db: HubDatabase) -> PipelineDefinition:
    sync_bundled_pipelines(temp_db)
    pipeline = await PipelineLoader(temp_db).load_pipeline("planning")
    assert pipeline is not None
    return pipeline


def _latest_execution_id(temp_db: HubDatabase) -> str:
    row = temp_db.fetchone(
        "SELECT id FROM pipeline_executions WHERE pipeline_name = %s "
        "ORDER BY created_at DESC LIMIT 1",
        ("planning",),
    )
    assert row is not None
    return str(row["id"])


SEAT_AGENTS = {
    "writer": "plan-writer",
    "adversary_split": "plan-adversary",
    "adversary_tab": "plan-adversary",
    "enhancer_split": "plan-enhancer",
    "enhancer_tab": "plan-enhancer",
}


async def test_runbook_syncs_bundled_and_tagged(temp_db: HubDatabase) -> None:
    sync_bundled_pipelines(temp_db)

    row = PipelineDefinitionManager(temp_db).get_by_name("planning")
    assert row is not None
    assert {"gobby", "runbook"} <= set(row.tags or [])

    pipeline = await PipelineLoader(temp_db).load_pipeline("planning")
    assert pipeline is not None
    assert "runbook" in pipeline.tags
    assert pipeline.resume_on_restart is True
    assert set(pipeline.inputs) == {
        "workspace",
        "writer_title",
        "enhancer_title",
        "adversary_title",
        "seats",
    }
    steps = {step.id: step for step in pipeline.steps}
    assert list(steps) == ["guard", *SEAT_AGENTS]
    guard = steps["guard"].mcp
    assert guard is not None
    assert (guard.server, guard.tool) == ("gobby-agents", "check_runbook_seats")
    for step_id, agent in SEAT_AGENTS.items():
        seat = steps[step_id].mcp
        assert seat is not None
        assert (seat.server, seat.tool) == ("gobby-agents", "spawn_agent")
        assert seat.arguments is not None
        assert seat.arguments["agent"] == agent
        assert seat.arguments["reserved_run_id"] == "${{ invocation_id }}"
        assert "role" not in str(seat.arguments["prompt"]).lower()
    assert not any("role" in name for name in pipeline.inputs)


async def test_partial_deploy_relaunches_missing_seat(temp_db: HubDatabase) -> None:
    pipeline = await _planning(temp_db)

    full = RunbookAgentsProxy()
    deployed = await _executor(temp_db, full).execute(pipeline, {"workspace": "ws-1"}, PROJECT_ID)
    assert deployed.status == ExecutionStatus.COMPLETED
    guard = full.calls[0]["arguments"]
    assert guard["requested"] == "writer,enhancer,adversary"
    assert [seat["agent"] for seat in guard["catalogue"]] == [
        "plan-writer",
        "plan-enhancer",
        "plan-adversary",
    ]
    writer, adversary, enhancer = full.spawns()
    assert writer["placement"] == {"tab": {"workspace": "ws-1", "title": "Plan Writer"}}
    assert adversary["placement"] == {
        "split": {"pane": "pane-2", "axis": "right", "title": "Plan Adversary"}
    }
    assert enhancer["placement"] == {
        "split": {"pane": "pane-2", "axis": "down", "title": "Plan Enhancer"}
    }
    for spawn, step_id in zip(
        (writer, adversary, enhancer), ("writer", "adversary_split", "enhancer_split"), strict=True
    ):
        assert spawn["reserved_run_id"] == step_invocation_id(deployed.id, step_id)

    partial = RunbookAgentsProxy(fail_agents=frozenset({"plan-adversary"}))
    with pytest.raises(RuntimeError, match="adversary_split"):
        await _executor(temp_db, partial).execute(pipeline, {"workspace": "ws-1"}, PROJECT_ID)
    manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    steps = {
        step.step_id: step
        for step in manager.get_steps_for_execution(_latest_execution_id(temp_db))
    }
    assert steps["writer"].status == StepStatus.COMPLETED
    assert json.loads(steps["writer"].output_json or "null")["run_id"] == "run-2"
    assert steps["adversary_split"].status == StepStatus.FAILED

    relaunch = RunbookAgentsProxy()
    result = await _executor(temp_db, relaunch).execute(
        pipeline, {"workspace": "ws-1", "seats": "adversary"}, PROJECT_ID
    )
    assert result.status == ExecutionStatus.COMPLETED
    assert relaunch.calls[0]["arguments"]["requested"] == "adversary"
    assert [spawn["agent"] for spawn in relaunch.spawns()] == ["plan-adversary"]
    assert relaunch.spawns()[0]["placement"] == {
        "tab": {"workspace": "ws-1", "title": "Plan Adversary"}
    }


def _assert_seats_parented(
    temp_db: HubDatabase, proxy: RunbookAgentsProxy, execution_id: str, parent_id: str
) -> None:
    child = SessionManager(temp_db).find_by_external_id(
        external_id=f"pipeline-{execution_id}", project_id=PROJECT_ID, source="pipeline"
    )
    assert child is not None
    assert child.parent_session_id == parent_id
    assert len(proxy.spawns()) == 3
    for call in proxy.calls:
        assert call["session_id"] == child.id
        assert call["ambient_session_id"] == child.id
        assert call["ambient_project_id"] == PROJECT_ID


async def test_entrypoint_parent_chain(temp_db: HubDatabase) -> None:
    await _planning(temp_db)
    caller = SessionManager(temp_db).register(
        external_id="runbook-caller", machine_id=None, source="claude", project_id=PROJECT_ID
    )

    mcp_proxy = RunbookAgentsProxy()
    started = await run_pipeline(
        PipelineLoader(temp_db),
        _executor(temp_db, mcp_proxy),
        "planning",
        {"workspace": "ws-1"},
        PROJECT_ID,
        session_id=caller.id,
    )
    assert started["success"] is True
    await _background_tasks_by_execution[started["execution_id"]]
    _assert_seats_parented(temp_db, mcp_proxy, started["execution_id"], caller.id)

    http_proxy = RunbookAgentsProxy()
    server = MagicMock()
    server.services.workflow_loader = PipelineLoader(temp_db)
    server.services.get_pipeline_executor.return_value = _executor(temp_db, http_proxy)
    app = FastAPI()
    app.include_router(create_pipelines_router(server))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/pipelines/run",
            json={"name": "planning", "project_id": PROJECT_ID, "inputs": {"workspace": "ws-2"}},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    _assert_seats_parented(
        temp_db, http_proxy, response.json()["execution_id"], system_session_id()
    )


class InterruptingProxy(RunbookAgentsProxy):
    """Cancels the run, as a daemon stop does, when it reaches the named agent's spawn."""

    def __init__(self, interrupt_agent: str) -> None:
        super().__init__()
        self.interrupt_agent = interrupt_agent

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
        enforce_workflow: bool,
    ) -> dict[str, Any]:
        reply = await super().call_tool(
            server, tool, arguments, session_id=session_id, enforce_workflow=enforce_workflow
        )
        if arguments.get("agent") == self.interrupt_agent:
            raise asyncio.CancelledError
        return reply


@pytest.mark.parametrize(
    ("interrupt_agent", "resumed_agents"),
    [
        ("plan-writer", ["plan-writer", "plan-adversary", "plan-enhancer"]),
        ("plan-adversary", ["plan-adversary", "plan-enhancer"]),
    ],
)
async def test_restart_reruns_on_same_child(
    temp_db: HubDatabase, interrupt_agent: str, resumed_agents: list[str]
) -> None:
    pipeline = await _planning(temp_db)
    caller = SessionManager(temp_db).register(
        external_id="runbook-caller", machine_id=None, source="claude", project_id=PROJECT_ID
    )
    before = InterruptingProxy(interrupt_agent)
    with pytest.raises(asyncio.CancelledError):
        await _executor(temp_db, before).execute(
            pipeline, {"workspace": "ws-1"}, PROJECT_ID, session_id=caller.id
        )
    execution_id = _latest_execution_id(temp_db)
    interrupted = before.spawns()[-1]

    after = RunbookAgentsProxy()
    manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    resumed = await resume_interrupted_pipelines(
        PipelineLoader(temp_db), _executor(temp_db, after), manager
    )
    assert resumed == [execution_id]
    await _background_tasks_by_execution[execution_id]

    finished = manager.get_execution(execution_id)
    assert finished is not None
    assert finished.status == ExecutionStatus.COMPLETED
    assert [call["tool"] for call in after.calls] == ["gobby-agents:spawn_agent"] * len(
        resumed_agents
    )
    assert [spawn["agent"] for spawn in after.spawns()] == resumed_agents
    assert after.spawns()[0]["reserved_run_id"] == interrupted["reserved_run_id"]
    assert after.calls[0]["session_id"] == before.calls[-1]["session_id"]
    _assert_child_parent(temp_db, execution_id, caller.id, after.calls[0]["session_id"])
    writer = {step.step_id: step for step in manager.get_steps_for_execution(execution_id)}[
        "writer"
    ]
    expected_writer_run = "run-1" if interrupt_agent == "plan-writer" else "run-2"
    assert json.loads(writer.output_json or "null")["run_id"] == expected_writer_run


def _assert_child_parent(
    temp_db: HubDatabase, execution_id: str, parent_id: str, child_id: str
) -> None:
    child = SessionManager(temp_db).find_by_external_id(
        external_id=f"pipeline-{execution_id}", project_id=PROJECT_ID, source="pipeline"
    )
    assert child is not None
    assert child.id == child_id
    assert child.parent_session_id == parent_id


def _mutate_current_definition(temp_db: HubDatabase, mutation: str) -> None:
    manager = PipelineDefinitionManager(temp_db)
    row = manager.get_by_name("planning")
    assert row is not None
    if mutation == "untagged":
        manager.update(row.id, tags=["gobby"])
    elif mutation == "changed":
        replacement = {
            "name": "planning",
            "type": "pipeline",
            "steps": [{"id": "noop", "exec": "true"}],
        }
        manager.update(row.id, definition_json=json.dumps(replacement))
    elif mutation == "deleted":
        assert manager.delete(row.id)


@pytest.mark.parametrize("mutation", ["unchanged", "untagged", "changed", "deleted"])
async def test_failed_runbook_resume_refused(temp_db: HubDatabase, mutation: str) -> None:
    pipeline = await _planning(temp_db)
    proxy = RunbookAgentsProxy(fail_agents=frozenset({"plan-adversary"}))
    executor = _executor(temp_db, proxy)
    with pytest.raises(RuntimeError, match="adversary_split"):
        await executor.execute(pipeline, {"workspace": "ws-1"}, PROJECT_ID)
    execution_id = _latest_execution_id(temp_db)
    manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)

    def snapshot() -> tuple[Any, ...]:
        execution = manager.get_execution(execution_id)
        assert execution is not None
        steps = manager.get_steps_for_execution(execution_id)
        return (
            execution.status,
            execution.outputs_json,
            [(step.step_id, step.status, step.output_json, step.error) for step in steps],
        )

    before = snapshot()
    _mutate_current_definition(temp_db, mutation)
    calls_before = len(proxy.calls)

    result = await resume_pipeline(
        PipelineLoader(temp_db), executor, manager, execution_id, PROJECT_ID
    )

    assert result["success"] is False
    assert result["error_code"] == RUNBOOK_RESUME_REFUSED
    assert "seats" in result["error"]
    assert execution_id not in _background_tasks_by_execution
    assert snapshot() == before
    assert before[0] == ExecutionStatus.FAILED
    assert len(proxy.calls) == calls_before
