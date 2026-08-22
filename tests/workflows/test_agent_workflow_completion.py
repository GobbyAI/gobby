"""Tests for engine-side completion of agent-scoped step workflows."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.tmux import TmuxConfig
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance

pytestmark = pytest.mark.unit

# Session/project/instance id columns are native uuid in PostgreSQL; synthetic
# ids like AGENT_SESSION_ID would fail with `invalid input syntax for type uuid`.
AGENT_SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPANSION_QA_AGENT_PATH = (
    PROJECT_ROOT / "src/gobby/install/shared/workflows/agents/expansion-qa.yaml"
)


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


def _create_session(db: HubDatabase, session_id: str) -> None:
    db.execute(
        """
        INSERT INTO projects (id, name, created_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        (PROJECT_ID, "test-project"),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (session_id, "ext-1", "21000000-0000-4000-8000-000000000001", "claude", PROJECT_ID),
    )


def _register_agent_workflow(
    db: HubDatabase,
    *,
    session_id: str = AGENT_SESSION_ID,
    workflow_name: str = "plan-adversary-steps",
    review_tool: str = "approve_review",
    review_success_handlers: list[dict[str, object]] | None = None,
    review_error_handlers: list[dict[str, object]] | None = None,
) -> AgentStepInstanceManager:
    _create_session(db, session_id)
    instance_manager = AgentStepInstanceManager(db)

    workflow_data = {
        "name": workflow_name,
        "version": "1.0",
        "enabled": True,
        "variables": {"review_complete": False},
        "steps": [
            {
                "name": "review",
                "allowed_tools": "all",
                "on_mcp_success": review_success_handlers
                or [
                    {
                        "server": "gobby-tasks-ops",
                        "tool": review_tool,
                        "action": "set_variable",
                        "variable": "review_complete",
                        "value": True,
                    }
                ],
                "on_mcp_error": review_error_handlers or [],
                "transitions": [{"to": "terminate", "when": "vars.review_complete"}],
            },
            {
                "name": "terminate",
                "allowed_tools": [
                    "mcp__gobby__call_tool",
                    "mcp__gobby__list_mcp_servers",
                    "mcp__gobby__list_tools",
                    "mcp__gobby__get_tool_schema",
                ],
                "allowed_mcp_tools": ["gobby-agents:end_agent_run"],
            },
        ],
        "exit_condition": "current_step == 'terminate'",
    }

    instance_manager.save(
        build_step_instance(
            AgentDefinitionBody(
                prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                name=workflow_name,
                step_workflow=AgentStepWorkflowBody.model_validate(
                    {
                        "variables": {},
                        "exit_condition": "current_step == 'terminate'",
                        "steps": workflow_data["steps"],
                    }
                ),
            ),
            session_id=session_id,
            step_workflow_id=None,
            variables={"review_complete": False},
            current_step="review",
        )
    )
    return instance_manager


def _register_qa_reviewer_workflow(
    db: HubDatabase,
    *,
    session_id: str = AGENT_SESSION_ID,
) -> AgentStepInstanceManager:
    _create_session(db, session_id)
    instance_manager = AgentStepInstanceManager(db)
    agent_path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/qa-reviewer.yaml"
    )
    agent = yaml.safe_load(agent_path.read_text(encoding="utf-8"))
    workflow_name = "qa-reviewer-steps"
    variables = dict(agent["step_workflow"]["variables"])
    variables.update(
        {
            "task_claimed": True,
            "required_skills_loaded": True,
            "review_complete": False,
        }
    )
    instance_manager.save(
        build_step_instance(
            AgentDefinitionBody(
                prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                name=workflow_name,
                step_workflow=AgentStepWorkflowBody.model_validate(agent["step_workflow"]),
            ),
            session_id=session_id,
            step_workflow_id=None,
            variables=variables,
            current_step="review",
        )
    )
    return instance_manager


def _register_expansion_qa_workflow(
    db: HubDatabase,
    *,
    session_id: str = AGENT_SESSION_ID,
) -> AgentStepInstanceManager:
    _create_session(db, session_id)
    instance_manager = AgentStepInstanceManager(db)
    agent_data = yaml.safe_load(EXPANSION_QA_AGENT_PATH.read_text(encoding="utf-8"))
    agent_body = AgentDefinitionBody.model_validate(agent_data)
    instance_manager.save(
        build_step_instance(
            agent_body,
            session_id=session_id,
            step_workflow_id=None,
            variables=dict(agent_body.step_workflow.variables if agent_body.step_workflow else {}),
            current_step="coverage_check",
        )
    )
    return instance_manager


def _after_tool_event(
    *,
    session_id: str = AGENT_SESSION_ID,
    source: SessionSource = SessionSource.CLAUDE,
    mcp_server: str = "gobby-tasks-ops",
    mcp_tool: str = "approve_review",
    tool_arguments: dict[str, object] | None = None,
    tool_output: object | None = None,
    tool_response: object | None = None,
) -> HookEvent:
    tool_input: dict[str, object] = {
        "server_name": mcp_server,
        "tool_name": mcp_tool,
    }
    if tool_arguments is not None:
        tool_input["arguments"] = tool_arguments
    data: dict[str, object] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": tool_input,
    }
    if tool_output is not None:
        data["tool_output"] = tool_output
    if tool_response is not None:
        data["tool_response"] = tool_response
    if "tool_output" not in data and "tool_response" not in data:
        data["tool_output"] = {"success": True}

    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=session_id,
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={},
    )


class TestAgentWorkflowCompletion:
    @pytest.mark.asyncio
    async def test_exit_condition_terminalizes_agent_run_through_lifecycle_cleanup(
        self, db: HubDatabase
    ) -> None:
        _register_agent_workflow(db)
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(
            id="ff807256-1906-55de-b7b3-94163bb18352"
        )
        runner.agent_lifecycle_monitor = MagicMock()
        runner.agent_lifecycle_monitor.terminalize_successful_run = AsyncMock(return_value=True)
        runner.complete_run.return_value = True
        runner.run_storage.db = db
        runner.get_run.return_value = MagicMock(status="success", error=None)
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()

        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)
        variables: dict[str, object] = {}

        response = await engine.evaluate(
            _after_tool_event(), session_id=AGENT_SESSION_ID, variables=variables
        )

        assert variables["step_workflow_complete"] is True
        assert response.decision == "allow"
        runner.complete_run.assert_not_called()
        runner.agent_lifecycle_monitor.terminalize_successful_run.assert_awaited_once_with(
            "ff807256-1906-55de-b7b3-94163bb18352",
            notify_result={
                "status": "success",
                "run_id": "ff807256-1906-55de-b7b3-94163bb18352",
                "via": "workflow_terminate",
                "workflow": "plan-adversary-steps",
            },
            message="Agent ff807256-1906-55de-b7b3-94163bb18352 completed via workflow terminate",
        )
        completion_registry.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_epic_review_complete_stage_success_transitions_to_terminate(
        self, db: HubDatabase
    ) -> None:
        instance_manager = _register_agent_workflow(
            db,
            workflow_name="epic-reviewer",
            review_tool="complete_stage",
        )
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(
            id="ff807256-1906-55de-b7b3-94163bb18352"
        )
        runner.complete_run.return_value = True
        runner.run_storage.db = db
        runner.get_run.return_value = MagicMock(status="success", error=None)
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()
        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)
        variables: dict[str, object] = {}

        response = await engine.evaluate(
            _after_tool_event(
                mcp_tool="complete_stage",
                tool_arguments={
                    "stage_name": "epic_qa",
                    "validation_override_reason": "epic_qa approved by epic-reviewer",
                },
            ),
            session_id=AGENT_SESSION_ID,
            variables=variables,
        )

        instance = instance_manager.get_for_session(AGENT_SESSION_ID)
        assert instance is None
        assert variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert response.context is not None
        completion_registry.notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exit_condition_noops_for_non_agent_session(self, db: HubDatabase) -> None:
        _register_agent_workflow(db)
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = None
        runner.get_run_id_by_session.return_value = None
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()

        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)
        variables: dict[str, object] = {}

        response = await engine.evaluate(
            _after_tool_event(), session_id=AGENT_SESSION_ID, variables=variables
        )

        assert variables["step_workflow_complete"] is True
        assert response.decision == "allow"
        runner.complete_run.assert_not_called()
        completion_registry.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_codex_mcp_envelope_keeps_review_step_open(self, db: HubDatabase) -> None:
        instance_manager = _register_agent_workflow(
            db,
            review_tool="reject_review",
        )
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(
            id="ff807256-1906-55de-b7b3-94163bb18352"
        )
        runner.complete_run.return_value = True
        runner.run_storage.db = db
        runner.get_run.return_value = MagicMock(status="success", error=None)
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()

        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)
        variables: dict[str, object] = {}

        failed_event = _after_tool_event(
            source=SessionSource.CODEX,
            mcp_tool="reject_review",
            tool_response={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": False, "error": "Invalid arguments"}),
                    }
                ],
                "structuredContent": {
                    "success": False,
                    "error": "Invalid arguments",
                },
                "isError": False,
            },
        )
        response = await engine.evaluate(
            failed_event,
            session_id=AGENT_SESSION_ID,
            variables=variables,
        )

        instance = instance_manager.get_for_session(AGENT_SESSION_ID)
        assert instance is not None
        assert instance.current_step == "review"
        assert instance.variables["review_complete"] is False
        assert response.context is None
        completion_registry.notify.assert_not_awaited()

        success_event = _after_tool_event(
            source=SessionSource.CODEX,
            mcp_tool="reject_review",
            tool_response={
                "content": [{"type": "text", "text": json.dumps({"success": True})}],
                "structuredContent": {"success": True},
                "isError": False,
            },
        )
        response = await engine.evaluate(
            success_event,
            session_id=AGENT_SESSION_ID,
            variables=variables,
        )

        instance = instance_manager.get_for_session(AGENT_SESSION_ID)
        assert instance is None
        assert variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert response.context is not None
        completion_registry.notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closed_review_target_error_completes_plan_adversary_workflow(
        self, db: HubDatabase
    ) -> None:
        instance_manager = _register_agent_workflow(
            db,
            review_tool="reject_review",
            review_error_handlers=[
                {
                    "server": "gobby-tasks-ops",
                    "tool": "reject_review",
                    "when": "'closed' in str(tool_output)",
                    "action": "set_variable",
                    "variable": "review_complete",
                    "value": True,
                }
            ],
        )
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(
            id="ff807256-1906-55de-b7b3-94163bb18352"
        )
        runner.complete_run.return_value = True
        runner.run_storage.db = db
        runner.get_run.return_value = MagicMock(status="success", error=None)
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()

        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)
        variables: dict[str, object] = {}

        event = _after_tool_event(
            source=SessionSource.CODEX,
            mcp_tool="reject_review",
            tool_output={
                "success": True,
                "result": {
                    "error": (
                        "Cannot reject review for task with status 'closed'. "
                        "Task must be in 'needs_review' or 'in_progress' status."
                    )
                },
            },
        )

        response = await engine.evaluate(event, session_id=AGENT_SESSION_ID, variables=variables)

        instance = instance_manager.get_for_session(AGENT_SESSION_ID)
        assert instance is None
        assert variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert response.context is not None
        completion_registry.notify.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verdict_tool", ["approve_review", "reject_review"])
    async def test_expansion_qa_verdict_terminalizes_generated_step_workflow(
        self, db: HubDatabase, verdict_tool: str
    ) -> None:
        instance_manager = _register_expansion_qa_workflow(db)
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(
            id="ff807256-1906-55de-b7b3-94163bb18352"
        )
        runner.agent_lifecycle_monitor = MagicMock()
        runner.agent_lifecycle_monitor.terminalize_successful_run = AsyncMock(return_value=True)
        runner.complete_run.return_value = True
        runner.run_storage.db = db
        runner.get_run.return_value = MagicMock(status="success", error=None)
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()
        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)
        variables: dict[str, object] = {}

        coverage_response = await engine.evaluate(
            _after_tool_event(
                mcp_tool="run_expansion_qa_coverage",
                tool_output={"success": True, "result": {"review_action": verdict_tool}},
            ),
            session_id=AGENT_SESSION_ID,
            variables=variables,
        )

        instance = instance_manager.get_for_session(AGENT_SESSION_ID)
        assert instance is not None
        assert instance.current_step == "qa_check"
        assert instance.variables["coverage_result_saved"] is True
        assert instance.variables["qa_result_saved"] is False
        assert "coverage_check -> qa_check" in (coverage_response.context or "")

        save_response = await engine.evaluate(
            _after_tool_event(
                mcp_tool="save_expansion_qa_result",
                tool_output={"success": True},
            ),
            session_id=AGENT_SESSION_ID,
            variables=variables,
        )

        instance = instance_manager.get_for_session(AGENT_SESSION_ID)
        assert instance is not None
        assert instance.current_step == "qa_check"
        assert instance.variables["qa_result_saved"] is True
        assert instance.variables["review_complete"] is False
        assert "step_workflow_complete" not in variables
        assert save_response.context is None

        verdict_response = await engine.evaluate(
            _after_tool_event(
                mcp_tool=verdict_tool,
                tool_arguments={"stage_name": "expansion"},
                tool_output={"success": True},
            ),
            session_id=AGENT_SESSION_ID,
            variables=variables,
        )

        assert instance_manager.get_for_session(AGENT_SESSION_ID) is None
        assert variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert "qa_check -> terminate" in (verdict_response.context or "")
        runner.complete_run.assert_not_called()
        runner.agent_lifecycle_monitor.terminalize_successful_run.assert_awaited_once_with(
            "ff807256-1906-55de-b7b3-94163bb18352",
            notify_result={
                "status": "success",
                "run_id": "ff807256-1906-55de-b7b3-94163bb18352",
                "via": "workflow_terminate",
                "workflow": "expansion-qa",
            },
            message="Agent ff807256-1906-55de-b7b3-94163bb18352 completed via workflow terminate",
        )
        completion_registry.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_qa_reviewer_stale_get_task_result_transitions_to_terminate(
        self, db: HubDatabase
    ) -> None:
        instance_manager = _register_qa_reviewer_workflow(db)
        engine = RuleEngine(db)
        variables: dict[str, object] = {}

        response = await engine.evaluate(
            _after_tool_event(
                mcp_server="gobby-tasks",
                mcp_tool="get_task",
                tool_output={
                    "success": True,
                    "result": {
                        "state": {
                            "is_closed": False,
                            "current_stage": {
                                "name": "merge",
                                "state": "in_progress",
                            },
                        }
                    },
                },
            ),
            session_id=AGENT_SESSION_ID,
            variables=variables,
        )

        instance = instance_manager.get_for_session(AGENT_SESSION_ID)
        assert instance is not None
        assert instance.current_step == "terminate"
        assert instance.variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert response.context is not None
        assert "review -> terminate" in response.context

    @pytest.mark.asyncio
    async def test_parent_wait_unblocks_without_end_agent_run_tool_call(
        self, db: HubDatabase
    ) -> None:
        _register_agent_workflow(db)
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(
            id="ff807256-1906-55de-b7b3-94163bb18352"
        )
        runner.complete_run.return_value = True
        runner.run_storage.db = db
        runner.get_run.return_value = MagicMock(status="success", error=None)
        wake_callback = AsyncMock(return_value={"ism_persisted": True})
        completion_registry = CompletionEventRegistry(wake_callback=wake_callback)
        completion_registry.register(
            "ff807256-1906-55de-b7b3-94163bb18352",
            subscribers=[AGENT_SESSION_ID],
        )

        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)

        # No end_agent_run tool call is issued in this test; workflow termination
        # must still wake the parent wait path immediately.
        await engine.evaluate(_after_tool_event(), session_id=AGENT_SESSION_ID, variables={})

        wake_callback.assert_awaited_once()
        result = wake_callback.call_args.args[2]
        assert result["status"] == "success"
        assert result["via"] == "workflow_terminate"
        assert result["workflow"] == "plan-adversary-steps"
        assert not completion_registry.is_registered("ff807256-1906-55de-b7b3-94163bb18352")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_workflow_termination_cleans_child_not_dispatcher_launcher(
        self, db: HubDatabase
    ) -> None:
        db.execute(
            """
            INSERT INTO projects (id, name, created_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO NOTHING
            """,
            (PROJECT_ID, "test-project"),
        )
        sessions = SessionManager(db)
        parent = sessions.register(
            external_id="dispatcher-launcher-project-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="dispatcher_launcher",
            project_id=PROJECT_ID,
            title="Dispatcher Launcher",
        )
        child = sessions.register(
            external_id="child-session",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=PROJECT_ID,
            parent_session_id=parent.id,
            agent_depth=1,
        )
        task = LocalTaskManager(db).create_task(
            project_id=PROJECT_ID,
            title="Workflow-owned task",
            validation_criteria="Workflow termination cleanup is verified.",
        )
        mutex = TaskDispatchMutexManager(db)
        mutex.acquire_mutex(
            task.id,
            holder="dispatcher",
            kind="spawn_agent",
            run_id="ff807256-1906-55de-b7b3-94163bb18352",
            ttl_seconds=300,
        )
        _register_agent_workflow(db, session_id=child.id)

        run_manager = LocalAgentRunManager(db)
        run = run_manager.create(
            parent_session_id=parent.id,
            child_session_id=child.id,
            provider="codex",
            prompt="do work",
            run_id="ff807256-1906-55de-b7b3-94163bb18352",
            task_id=task.id,
        )
        # Spawn always binds the session back-pointer pre-launch; terminal
        # session expiry is back-pointer-authoritative.
        sessions.update_terminal_pickup_metadata(child.id, agent_run_id=run.id)
        run_manager.start(run.id)
        completion_registry = CompletionEventRegistry()
        completion_registry.register(run.id, subscribers=[])
        runner = MagicMock()
        runner.run_storage = run_manager
        runner.agent_lifecycle_monitor = AgentLifecycleMonitor(
            detection_registry=MagicMock(),
            agent_run_manager=run_manager,
            db=db,
            session_manager=sessions,
            completion_registry=completion_registry,
            task_manager=LocalTaskManager(db),
            tmux_config=TmuxConfig(),
        )
        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)

        await engine.evaluate(
            _after_tool_event(session_id=child.id),
            session_id=child.id,
            variables={},
        )

        completed = run_manager.get(run.id)
        assert completed is not None
        assert completed.status == "success"
        assert completed.parent_session_id == parent.id
        assert not completion_registry.is_registered(run.id)
        parent_lookup = sessions.get(parent.id)
        child_lookup = sessions.get(child.id)
        assert parent_lookup is not None
        assert child_lookup is not None
        assert parent_lookup.status == "active"
        assert child_lookup.status == "expired"
        assert mutex.get_mutex(task.id) is None
        assert AgentStepInstanceManager(db).get_for_session(child.id) is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.parametrize("with_lifecycle_monitor", [True, False])
    async def test_workflow_completion_removes_acknowledged_subscription(
        self,
        db: HubDatabase,
        with_lifecycle_monitor: bool,
    ) -> None:
        db.execute(
            """
            INSERT INTO projects (id, name, created_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO NOTHING
            """,
            (PROJECT_ID, "test-project"),
        )
        sessions = SessionManager(db)
        parent = sessions.register(
            external_id=f"workflow-parent-{with_lifecycle_monitor}",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=PROJECT_ID,
        )
        child = sessions.register(
            external_id=f"workflow-child-{with_lifecycle_monitor}",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=PROJECT_ID,
            parent_session_id=parent.id,
            agent_depth=1,
        )
        _register_agent_workflow(db, session_id=child.id)

        run_manager = LocalAgentRunManager(db)
        run = run_manager.create(
            parent_session_id=parent.id,
            child_session_id=child.id,
            provider="codex",
            prompt="do work",
            run_id=str(uuid.uuid4()),
        )
        run_manager.start(run.id)
        wake_callback = AsyncMock(return_value={"ism_persisted": True})
        completion_registry = CompletionEventRegistry(wake_callback=wake_callback)
        completion_registry.register(run.id, subscribers=[parent.id])
        subscribers = CompletionSubscriberManager(db)
        subscribers.add_completion_subscribers(run.id, [parent.id])

        runner = MagicMock()
        runner.run_storage = run_manager
        runner.complete_run = run_manager.complete
        runner.get_run = run_manager.get
        runner.agent_lifecycle_monitor = (
            AgentLifecycleMonitor(
                detection_registry=MagicMock(),
                agent_run_manager=run_manager,
                db=db,
                session_manager=sessions,
                completion_registry=completion_registry,
                task_manager=LocalTaskManager(db),
                tmux_config=TmuxConfig(),
            )
            if with_lifecycle_monitor
            else None
        )
        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)

        await engine.evaluate(
            _after_tool_event(session_id=child.id),
            session_id=child.id,
            variables={},
        )

        assert subscribers.get_completion_subscribers(run.id) == []
        assert not completion_registry.is_registered(run.id)
        wake_callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_mcp_success_when_condition_checks_tool_argument(
        self, db: HubDatabase
    ) -> None:
        instance_manager = _register_agent_workflow(
            db,
            workflow_name="merge-orchestrator-test",
            review_tool="verify_in_worktree",
            review_success_handlers=[
                {
                    "server": "gobby-merge",
                    "tool": "verify_in_worktree",
                    "when": "tool_input.final is True",
                    "action": "set_variable",
                    "variable": "review_complete",
                    "value": True,
                }
            ],
        )
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(
            id="ff807256-1906-55de-b7b3-94163bb18352"
        )
        runner.complete_run.return_value = True
        runner.run_storage.db = db
        runner.get_run.return_value = MagicMock(status="success", error=None)
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()
        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)
        variables: dict[str, object] = {}

        await engine.evaluate(
            _after_tool_event(
                mcp_server="gobby-merge",
                mcp_tool="verify_in_worktree",
                tool_arguments={"final": False},
            ),
            session_id=AGENT_SESSION_ID,
            variables=variables,
        )

        instance = instance_manager.get_for_session(AGENT_SESSION_ID)
        assert instance is not None
        assert instance.current_step == "review"
        assert instance.variables["review_complete"] is False
        assert "review_complete" not in variables

        await engine.evaluate(
            _after_tool_event(
                mcp_server="gobby-merge",
                mcp_tool="verify_in_worktree",
                tool_arguments={"final": True},
            ),
            session_id=AGENT_SESSION_ID,
            variables=variables,
        )

        instance = instance_manager.get_for_session(AGENT_SESSION_ID)
        assert instance is None
        assert variables["review_complete"] is True
