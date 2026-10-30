"""Bundled `planning` runbook: sync, seat launches, restart, resume refusal."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gobby.mcp_proxy.tools.spawn_agent._seat_adoption import (
    authorize_pipeline_invocation,
    reconcile_pipeline_invocation,
)
from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
    RUNBOOK_RESUME_REFUSED,
    _background_tasks_by_execution,
    cancel_pipeline,
    resume_interrupted_pipelines,
    resume_pipeline,
    run_pipeline,
)
from gobby.servers.routes.pipelines import create_pipelines_router
from gobby.storage.agents import LocalAgentRunManager
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
            return {
                "success": True,
                "launch": arguments["requested"].split(","),
                "live": {},
                "tab_exists": False,
                "workspace_id": "ws-1",
                "tab_title": TAB_TITLE,
            }
        # Non-spawn tools (rebalance, task lookups, messages) succeed without a spawn payload.
        if tool != "spawn_agent":
            return {"success": True}
        if arguments["agent"] in self.fail_agents:
            return {"success": False, "error": "spawn refused"}
        # Ids number spawns, so the plan writer is run-1 whatever steps launch before it.
        index = len(self.spawns())
        return {
            "success": True,
            "run_id": f"run-{index}",
            "pane_ref": f"pane-{index}",
            "tab_ref": f"tab-{index}",
        }

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


async def _team_planning(temp_db: HubDatabase) -> PipelineDefinition:
    sync_bundled_pipelines(temp_db)
    pipeline = await PipelineLoader(temp_db).load_pipeline("team-planning")
    assert pipeline is not None
    return pipeline


def _latest_execution_id(temp_db: HubDatabase) -> str:
    row = temp_db.fetchone(
        "SELECT id FROM pipeline_executions WHERE pipeline_name = %s "
        "ORDER BY created_at DESC LIMIT 1",
        ("team-planning",),
    )
    assert row is not None
    return str(row["id"])


SEATS = ("plan-writer", "plan-adversary", "plan-enhancer")
SEATS_INPUT = ",".join(SEATS)
TAB_TITLE = "Team Planning - #25223"
RUN_INPUTS = {
    "workspace": "ws-1",
    "task": "#25223",
    "project_path": "/pilot/shared-checkout",
    "report_to": "gobby#14972",
    "seats": SEATS_INPUT,
}


async def test_runbook_syncs_bundled_and_tagged(temp_db: HubDatabase) -> None:
    sync_bundled_pipelines(temp_db)

    row = PipelineDefinitionManager(temp_db).get_by_name("team-planning")
    assert row is not None
    assert {"gobby", "runbook"} <= set(row.tags or [])

    pipeline = await PipelineLoader(temp_db).load_pipeline("team-planning")
    assert pipeline is not None
    assert "runbook" in pipeline.tags
    assert pipeline.resume_on_restart is True
    assert set(pipeline.inputs) == {
        "workspace",
        "task",
        "seat_pane",
        "project_path",
        "seats",
        "report_to",
        "plan_writer_provider",
        "plan_writer_model",
        "plan_writer_reasoning_effort",
        "plan_adversary_provider",
        "plan_adversary_model",
        "plan_adversary_reasoning_effort",
        "plan_enhancer_provider",
        "plan_enhancer_model",
        "plan_enhancer_reasoning_effort",
    }
    steps = {step.id: step for step in pipeline.steps}
    assert list(steps) == [
        "guard",
        "target",
        "planning-children-count",
        "planning-children",
        "planning-child",
        *SEATS,
        "notify-writer",
        "arrange",
    ]
    guard = steps["guard"].mcp
    assert guard is not None
    assert (guard.server, guard.tool) == ("gobby-agents", "check_runbook_seats")
    for agent in SEATS:
        seat = steps[agent].mcp
        assert seat is not None
        assert (seat.server, seat.tool) == ("gobby-agents", "spawn_agent")
        assert seat.arguments is not None
        assert seat.arguments["agent"] == agent
        assert seat.arguments["reserved_run_id"] == "${{ invocation_id }}"
        assert "role" not in str(seat.arguments["prompt"]).lower()
    assert not any("role" in name for name in pipeline.inputs)


async def test_partial_deploy_relaunches_missing_seat(temp_db: HubDatabase) -> None:
    pipeline = await _team_planning(temp_db)

    full = RunbookAgentsProxy()
    deployed = await _executor(temp_db, full).execute(pipeline, RUN_INPUTS, PROJECT_ID)
    assert deployed.status == ExecutionStatus.COMPLETED
    guard = full.calls[0]["arguments"]
    assert guard["requested"] == SEATS_INPUT
    assert [seat["agent"] for seat in guard["catalogue"]] == list(SEATS)
    writer, adversary, enhancer = full.spawns()
    assert writer["placement"] == {"tab": {"workspace": "ws-1", "title": TAB_TITLE}}
    assert adversary["placement"] == {
        "split": {"pane": "pane-1", "axis": "right", "title": "Plan Adversary"}
    }
    assert enhancer["placement"] == {
        "split": {"pane": "pane-1", "axis": "right", "title": "Plan Enhancer"}
    }
    for spawn, agent in zip((writer, adversary, enhancer), SEATS, strict=True):
        assert spawn["reserved_run_id"] == step_invocation_id(deployed.id, agent)

    partial = RunbookAgentsProxy(fail_agents=frozenset({"plan-adversary"}))
    with pytest.raises(RuntimeError, match="plan-adversary"):
        await _executor(temp_db, partial).execute(pipeline, RUN_INPUTS, PROJECT_ID)
    manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    steps = {
        step.step_id: step
        for step in manager.get_steps_for_execution(_latest_execution_id(temp_db))
    }
    assert steps["plan-writer"].status == StepStatus.COMPLETED
    assert json.loads(steps["plan-writer"].output_json or "null")["run_id"] == "run-1"
    assert steps["plan-adversary"].status == StepStatus.FAILED

    relaunch = RunbookAgentsProxy()
    result = await _executor(temp_db, relaunch).execute(
        pipeline, {**RUN_INPUTS, "seats": "plan-adversary"}, PROJECT_ID
    )
    assert result.status == ExecutionStatus.COMPLETED
    assert relaunch.calls[0]["arguments"]["requested"] == "plan-adversary"
    assert [spawn["agent"] for spawn in relaunch.spawns()] == ["plan-adversary"]
    assert relaunch.spawns()[0]["placement"] == {"tab": {"workspace": "ws-1", "title": TAB_TITLE}}


DEVTEAM_SEATS = ["dev-manager", "test-writer", "developer", "code-reviewer"]
LAYOUT_REFUSED = {
    "success": False,
    "error": "Workspace tools act only for a session or the operator's local token",
    "code": "forbidden",
}


def _layout_reply(arguments: dict[str, Any]) -> dict[str, Any]:
    """The tiled and skipped seats of a layout call, as rebalance_tab replies them."""
    seats = arguments.get("seats", [])
    return {
        "success": True,
        "tiled": [seat["name"] for seat in seats if seat["pane"]],
        "skipped": [
            {"seat": seat["name"], "reason": "pane_missing"} for seat in seats if not seat["pane"]
        ],
    }


class DevteamProxy(RunbookAgentsProxy):
    """Stub devteam proxy: the guard launches every seat; the layout write is refused unless asked.

    A seat in ``paneless`` spawns with a null pane_ref, as an adopted seat whose pane is gone does.
    """

    def __init__(
        self, *, paneless: frozenset[str] = frozenset(), layout_refused: bool = True
    ) -> None:
        super().__init__()
        self.paneless = paneless
        self.layout_refused = layout_refused

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
        enforce_workflow: bool,
    ) -> dict[str, Any]:
        self.calls.append({"tool": f"{server}:{tool}", "arguments": arguments})
        if tool == "check_runbook_seats":
            return {
                "success": True,
                "launch": list(DEVTEAM_SEATS),
                "live": {},
                "tab_exists": False,
                "workspace_id": "ws-1",
                "tab_title": "Dev Team - #25189",
            }
        if tool == "admit_team":
            return {
                "success": True,
                "task_id": arguments["task"],
                "branch": "task-branch",
                "project_path": "/tmp/devteam",
                "team": {"team_id": "team-1"},
            }
        if server == "gobby-workspaces":
            return dict(LAYOUT_REFUSED) if self.layout_refused else _layout_reply(arguments)
        index = len(self.calls)
        pane_ref = None if arguments.get("agent") in self.paneless else f"pane-{index}"
        return {
            "success": True,
            "run_id": f"run-{index}",
            "child_session_id": f"child-{index}",
            "pane_ref": pane_ref,
        }


async def test_devteam_run_completes_when_its_arrange_layout_is_refused(
    temp_db: HubDatabase,
) -> None:
    """A restart-resumed devteam run has no request principal, so rebalance_tab refuses it.

    The refusal ends the arrange step, not the run: the seats launch and the refusal is recorded.
    """
    sync_bundled_pipelines(temp_db)
    pipeline = await PipelineLoader(temp_db).load_pipeline("devteam")
    assert pipeline is not None
    inputs = {name: spec.get("default") for name, spec in pipeline.inputs.items()}
    inputs.update(
        task="task-1",
        workspace="devteam",
        report_to="report-session",
        reviewer_agent="code-reviewer",
    )

    proxy = DevteamProxy()
    execution = await _executor(temp_db, proxy).execute(pipeline, inputs, PROJECT_ID)

    assert execution.status == ExecutionStatus.COMPLETED
    assert [spawn["agent"] for spawn in proxy.spawns()] == DEVTEAM_SEATS
    manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    [arrange] = [
        step for step in manager.get_steps_for_execution(execution.id) if step.step_id == "arrange"
    ]
    assert "forbidden" in f"{arrange.output_json or ''}{arrange.error or ''}"


async def test_devteam_arrange_names_the_paneless_seat_it_skipped(temp_db: HubDatabase) -> None:
    """A resumed team whose developer pane is gone: the layout tiles the rest and names the skip."""
    sync_bundled_pipelines(temp_db)
    pipeline = await PipelineLoader(temp_db).load_pipeline("devteam")
    assert pipeline is not None
    inputs = {name: spec.get("default") for name, spec in pipeline.inputs.items()}
    inputs.update(
        task="task-1",
        workspace="devteam",
        report_to="report-session",
        reviewer_agent="code-reviewer",
    )

    proxy = DevteamProxy(paneless=frozenset({"developer"}), layout_refused=False)
    execution = await _executor(temp_db, proxy).execute(pipeline, inputs, PROJECT_ID)

    assert execution.status == ExecutionStatus.COMPLETED
    [layout] = [call for call in proxy.calls if call["tool"] == "gobby-workspaces:rebalance_tab"]
    seats = layout["arguments"]["seats"]
    assert [(seat["name"], seat["pane"] is None) for seat in seats] == [
        ("dev-manager", False),
        ("test-writer", False),
        ("developer", True),
        ("code-reviewer", False),
    ]
    manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    [arrange] = [
        step for step in manager.get_steps_for_execution(execution.id) if step.step_id == "arrange"
    ]
    output = json.loads(arrange.output_json or "null")
    assert output["tiled"] == ["dev-manager", "test-writer", "code-reviewer"]
    assert output["skipped"] == [{"seat": "developer", "reason": "pane_missing"}]


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
    await _team_planning(temp_db)
    caller = SessionManager(temp_db).register(
        external_id="runbook-caller", machine_id=None, source="claude", project_id=PROJECT_ID
    )

    mcp_proxy = RunbookAgentsProxy()
    started = await run_pipeline(
        PipelineLoader(temp_db),
        _executor(temp_db, mcp_proxy),
        "team-planning",
        RUN_INPUTS,
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
            json={
                "name": "team-planning",
                "project_id": PROJECT_ID,
                "inputs": {**RUN_INPUTS, "workspace": "ws-2"},
            },
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
    pipeline = await _team_planning(temp_db)
    caller = SessionManager(temp_db).register(
        external_id="runbook-caller", machine_id=None, source="claude", project_id=PROJECT_ID
    )
    before = InterruptingProxy(interrupt_agent)
    with pytest.raises(asyncio.CancelledError):
        await _executor(temp_db, before).execute(
            pipeline, RUN_INPUTS, PROJECT_ID, session_id=caller.id
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
    # The writer's adversary/enhancer notice runs once the resumed seats have launched; the
    # arrange step tiles the tab after that. Planning-child steps completed before the restart.
    assert [call["tool"] for call in after.calls] == [
        *["gobby-agents:spawn_agent"] * len(resumed_agents),
        "gobby-agents:send_message",
        "gobby-workspaces:rebalance_tab",
    ]
    assert [spawn["agent"] for spawn in after.spawns()] == resumed_agents
    assert after.spawns()[0]["reserved_run_id"] == interrupted["reserved_run_id"]
    assert after.calls[0]["session_id"] == before.calls[-1]["session_id"]
    _assert_child_parent(temp_db, execution_id, caller.id, after.calls[0]["session_id"])
    writer = {step.step_id: step for step in manager.get_steps_for_execution(execution_id)}[
        "plan-writer"
    ]
    # The writer is the first spawn of either attempt, so its run is run-1 in both.
    assert json.loads(writer.output_json or "null")["run_id"] == "run-1"


class ManyChildrenProxy(RunbookAgentsProxy):
    """Lists the task's children as the daemon does: rows up to limit, open_count in total."""

    def __init__(self, children: list[dict[str, Any]]) -> None:
        super().__init__()
        self.children = children

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
        if tool != "list_tasks":
            return reply
        limit = arguments.get("limit", 50)
        return {
            "success": True,
            "tasks": self.children[:limit],
            "count": min(limit, len(self.children)),
            "open_count": len(self.children),
        }


async def test_planning_child_past_the_first_fifty_children_is_reused(
    temp_db: HubDatabase,
) -> None:
    pipeline = await _team_planning(temp_db)
    children = [
        {"ref": f"#{26000 + index}", "category": "code", "title": f"Child {index}"}
        for index in range(54)
    ]
    children.append({"ref": "#26100", "category": "planning", "title": "Plan #25248"})
    proxy = ManyChildrenProxy(children)

    result = await _executor(temp_db, proxy).execute(pipeline, RUN_INPUTS, PROJECT_ID)

    assert result.status == ExecutionStatus.COMPLETED
    assert "gobby-tasks:create_task" not in [call["tool"] for call in proxy.calls]
    writer = proxy.spawns()[0]
    assert "#26100" in writer["prompt"]


@pytest.mark.parametrize("run_state", ["missing", "pending", "started"])
async def test_restart_reconciles_real_reserved_run(temp_db: HubDatabase, run_state: str) -> None:
    pipeline = await _team_planning(temp_db)
    before = InterruptingProxy("plan-adversary")
    with pytest.raises(asyncio.CancelledError):
        await _executor(temp_db, before).execute(pipeline, RUN_INPUTS, PROJECT_ID)
    execution_id = _latest_execution_id(temp_db)
    interrupted = before.spawns()[-1]
    run_id = interrupted["reserved_run_id"]
    sessions = SessionManager(temp_db)
    runs = LocalAgentRunManager(temp_db)
    if run_state != "missing":
        run = runs.create(
            parent_session_id=before.calls[-1]["session_id"],
            provider="claude",
            prompt="Interrupted placed seat",
            run_id=run_id,
        )
        if run_state == "started":
            assert runs.start(run.id) is not None
    else:
        assert runs.get(run_id) is None

    class ReconcileProxy(RunbookAgentsProxy):
        async def call_tool(
            self,
            server: str,
            tool: str,
            arguments: dict[str, Any],
            *,
            session_id: str | None,
            enforce_workflow: bool,
        ) -> dict[str, Any]:
            if server == "gobby-agents" and tool == "spawn_agent":
                assert session_id is not None
                caller = sessions.get(session_id)
                assert caller is not None
                reserved = arguments["reserved_run_id"]
                reply = authorize_pipeline_invocation(
                    temp_db,
                    caller,
                    parent_session_id=session_id,
                    project_id=PROJECT_ID,
                    reserved_run_id=reserved,
                ) or reconcile_pipeline_invocation(temp_db, sessions, caller, reserved)
                if reply is not None:
                    replies.append(reply)
                    return reply
            return await super().call_tool(
                server,
                tool,
                arguments,
                session_id=session_id,
                enforce_workflow=enforce_workflow,
            )

    replies: list[dict[str, Any]] = []
    manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    after = ReconcileProxy()
    resumed = await resume_interrupted_pipelines(
        PipelineLoader(temp_db), _executor(temp_db, after), manager
    )
    assert resumed == [execution_id]
    await _background_tasks_by_execution[execution_id]
    finished = manager.get_execution(execution_id)
    assert finished is not None
    if run_state == "missing":
        assert replies == []
        replayed = [spawn for spawn in after.spawns() if spawn["reserved_run_id"] == run_id]
        assert len(replayed) == 1 and replayed[0]["agent"] == "plan-adversary"
        assert finished.status == ExecutionStatus.COMPLETED
        return

    [reply] = replies
    assert "reserved_run_id is not a step invocation" not in reply.get("error", "")
    if run_state == "started":
        assert reply["success"] is True and reply["adopted"] is True
        assert reply["run_id"] == run_id
    else:
        assert reply["success"] is False
        assert run_id in reply["error"]
        assert "fresh" in reply["error"].lower() and "launch" in reply["error"].lower()
    assert finished.status == (
        ExecutionStatus.COMPLETED if run_state == "started" else ExecutionStatus.FAILED
    )
    assert all(spawn["reserved_run_id"] != run_id for spawn in after.spawns())


@pytest.mark.parametrize(
    "reason",
    ["load_error", "missing", "disabled", "not_resumable", "unusable_snapshot", "new_run"],
)
async def test_restart_closes_child_only_for_skipped_old_execution(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    pipeline = await _team_planning(temp_db)
    before = InterruptingProxy("plan-adversary")
    with pytest.raises(asyncio.CancelledError):
        await _executor(temp_db, before).execute(pipeline, RUN_INPUTS, PROJECT_ID)
    execution_id = _latest_execution_id(temp_db)
    manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)
    execution = manager.get_execution(execution_id)
    assert execution is not None
    sessions = SessionManager(temp_db)
    child = sessions.find_by_external_id(
        external_id=f"pipeline-{execution_id}", project_id=PROJECT_ID, source="pipeline"
    )
    assert child is not None and child.status == "active"

    async def load(*args: object, **kwargs: object) -> PipelineDefinition | None:
        if reason == "load_error":
            raise ValueError("Definition could not be loaded")
        if reason in {"missing", "new_run"}:
            return None
        return pipeline.model_copy(
            update={"enabled": reason != "disabled", "resume_on_restart": reason != "not_resumable"}
        )

    if reason == "unusable_snapshot":
        with temp_db.transaction() as conn:
            conn.execute(
                "UPDATE pipeline_executions SET definition_json = %s WHERE id = %s",
                ("{}", execution_id),
            )
    loader = PipelineLoader(temp_db)
    monkeypatch.setattr(loader, "load_pipeline", load)
    after = RunbookAgentsProxy()
    resumed = await resume_interrupted_pipelines(
        loader,
        _executor(temp_db, after),
        manager,
        created_before=execution.created_at if reason == "new_run" else None,
    )
    assert resumed == [] and after.calls == []
    retained = sessions.get(child.id)
    assert retained is not None
    assert retained.status == ("active" if reason == "new_run" else "deleted")


class HoldingProxy(RunbookAgentsProxy):
    """Holds one seat's spawn open so a cancel lands mid-step."""

    def __init__(self, hold_agent: str) -> None:
        super().__init__()
        self.hold_agent = hold_agent
        self.held = asyncio.Event()

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
        enforce_workflow: bool,
    ) -> dict[str, Any]:
        if arguments.get("agent") == self.hold_agent:
            self.held.set()
            await asyncio.Event().wait()
        return await super().call_tool(
            server, tool, arguments, session_id=session_id, enforce_workflow=enforce_workflow
        )


async def test_operator_cancel_closes_resumable_child(temp_db: HubDatabase) -> None:
    pipeline = await _team_planning(temp_db)
    assert pipeline.resume_on_restart is True
    proxy = HoldingProxy("plan-adversary")
    started = await run_pipeline(
        PipelineLoader(temp_db), _executor(temp_db, proxy), "team-planning", RUN_INPUTS, PROJECT_ID
    )
    execution_id = started["execution_id"]
    await asyncio.wait_for(proxy.held.wait(), timeout=10)
    manager = LocalPipelineExecutionManager(temp_db, project_id=PROJECT_ID)

    result = await cancel_pipeline(manager, execution_id)

    assert result["success"] is True, result
    cancelled = manager.get_execution(execution_id)
    assert cancelled is not None and cancelled.status == ExecutionStatus.CANCELLED
    child = SessionManager(temp_db).find_by_external_id(
        external_id=f"pipeline-{execution_id}", project_id=PROJECT_ID, source="pipeline"
    )
    assert child is not None
    assert child.status == "deleted"


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
    row = manager.get_by_name("team-planning")
    assert row is not None
    if mutation == "untagged":
        manager.update(row.id, tags=["gobby"])
    elif mutation == "changed":
        replacement = {
            "name": "team-planning",
            "type": "pipeline",
            "steps": [{"id": "noop", "exec": "true"}],
        }
        manager.update(row.id, definition_json=json.dumps(replacement))
    elif mutation == "deleted":
        assert manager.delete(row.id)


@pytest.mark.parametrize("mutation", ["unchanged", "untagged", "changed", "deleted"])
async def test_failed_runbook_resume_refused(temp_db: HubDatabase, mutation: str) -> None:
    pipeline = await _team_planning(temp_db)
    proxy = RunbookAgentsProxy(fail_agents=frozenset({"plan-adversary"}))
    executor = _executor(temp_db, proxy)
    with pytest.raises(RuntimeError, match="plan-adversary"):
        await executor.execute(pipeline, RUN_INPUTS, PROJECT_ID)
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
