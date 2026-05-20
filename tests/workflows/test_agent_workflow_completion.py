"""Tests for engine-side completion of agent-scoped step workflows."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path: Path) -> LocalDatabase:
    database = LocalDatabase(tmp_path / "test_agent_workflow_completion.db")
    run_migrations(database)
    return database


def _create_session(db: LocalDatabase, session_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (?, ?, datetime('now'))",
        ("project-1", "test-project"),
    )
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        (session_id, "ext-1", "machine-1", "claude", "project-1"),
    )


def _register_agent_workflow(
    db: LocalDatabase,
    *,
    session_id: str = "agent-session",
    workflow_name: str = "plan-adversary-steps",
    review_tool: str = "approve_review",
    review_success_handlers: list[dict[str, object]] | None = None,
    review_error_handlers: list[dict[str, object]] | None = None,
) -> WorkflowInstanceManager:
    _create_session(db, session_id)
    manager = LocalWorkflowDefinitionManager(db)
    instance_manager = WorkflowInstanceManager(db)

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

    manager.create(
        name=workflow_name,
        definition_json=json.dumps(workflow_data),
        workflow_type="workflow",
        priority=100,
        enabled=True,
    )
    instance_manager.save_instance(
        WorkflowInstance(
            id=f"inst-{session_id}-{workflow_name}",
            session_id=session_id,
            workflow_name=workflow_name,
            enabled=True,
            priority=100,
            current_step="review",
            step_entered_at=datetime.now(UTC),
            variables={"review_complete": False},
        )
    )
    return instance_manager


def _register_qa_reviewer_workflow(
    db: LocalDatabase,
    *,
    session_id: str = "agent-session",
) -> WorkflowInstanceManager:
    _create_session(db, session_id)
    manager = LocalWorkflowDefinitionManager(db)
    instance_manager = WorkflowInstanceManager(db)
    agent_path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/qa-reviewer.yaml"
    )
    agent = yaml.safe_load(agent_path.read_text(encoding="utf-8"))
    workflow_name = "qa-reviewer-steps"
    workflow_data = {
        "name": workflow_name,
        "version": "1.0",
        "enabled": True,
        "variables": agent["step_variables"],
        "steps": agent["steps"],
        "exit_condition": "current_step == 'terminate'",
    }

    manager.create(
        name=workflow_name,
        definition_json=json.dumps(workflow_data),
        workflow_type="workflow",
        priority=100,
        enabled=True,
    )
    variables = dict(agent["step_variables"])
    variables.update(
        {
            "task_claimed": True,
            "required_skills_loaded": True,
            "review_complete": False,
        }
    )
    instance_manager.save_instance(
        WorkflowInstance(
            id=f"inst-{session_id}-{workflow_name}",
            session_id=session_id,
            workflow_name=workflow_name,
            enabled=True,
            priority=100,
            current_step="review",
            step_entered_at=datetime.now(UTC),
            variables=variables,
        )
    )
    return instance_manager


def _after_tool_event(
    *,
    session_id: str = "agent-session",
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
        self, db: LocalDatabase
    ) -> None:
        _register_agent_workflow(db)
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(id="run-123")
        runner.agent_lifecycle_monitor = MagicMock()
        runner.agent_lifecycle_monitor.terminalize_successful_run = AsyncMock(return_value=True)
        runner.complete_run.return_value = True
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()

        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)
        variables: dict[str, object] = {}

        await engine.evaluate(_after_tool_event(), session_id="agent-session", variables=variables)

        assert variables["step_workflow_complete"] is True
        runner.complete_run.assert_not_called()
        runner.agent_lifecycle_monitor.terminalize_successful_run.assert_awaited_once_with(
            "run-123",
            notify_result={
                "status": "success",
                "run_id": "run-123",
                "via": "workflow_terminate",
                "workflow": "plan-adversary-steps",
            },
            message="Agent run-123 completed via workflow terminate",
        )
        completion_registry.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_holistic_review_complete_stage_success_transitions_to_terminate(
        self, db: LocalDatabase
    ) -> None:
        instance_manager = _register_agent_workflow(
            db,
            workflow_name="holistic-reviewer",
            review_tool="complete_stage",
        )
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(id="run-123")
        runner.complete_run.return_value = True
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()
        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)
        variables: dict[str, object] = {}

        response = await engine.evaluate(
            _after_tool_event(
                mcp_tool="complete_stage",
                tool_arguments={
                    "stage_name": "holistic_qa",
                    "validation_override_reason": "holistic_qa approved by holistic-reviewer",
                },
            ),
            session_id="agent-session",
            variables=variables,
        )

        instance = instance_manager.get_instance("agent-session", "holistic-reviewer")
        assert instance is None
        assert variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert response.context is not None
        completion_registry.notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exit_condition_noops_for_non_agent_session(self, db: LocalDatabase) -> None:
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

        await engine.evaluate(_after_tool_event(), session_id="agent-session", variables=variables)

        assert variables["step_workflow_complete"] is True
        runner.complete_run.assert_not_called()
        assert runner.complete_run.call_count == 0
        assert not runner.complete_run.called
        completion_registry.notify.assert_not_awaited()
        assert completion_registry.notify.await_count == 0
        assert completion_registry.notify.await_args is None

    @pytest.mark.asyncio
    async def test_failed_codex_mcp_envelope_keeps_review_step_open(
        self, db: LocalDatabase
    ) -> None:
        instance_manager = _register_agent_workflow(
            db,
            review_tool="reject_review",
        )
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(id="run-123")
        runner.complete_run.return_value = True
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
            session_id="agent-session",
            variables=variables,
        )

        instance = instance_manager.get_instance("agent-session", "plan-adversary-steps")
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
            session_id="agent-session",
            variables=variables,
        )

        instance = instance_manager.get_instance("agent-session", "plan-adversary-steps")
        assert instance is None
        assert variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert response.context is not None
        completion_registry.notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closed_review_target_error_completes_plan_adversary_workflow(
        self, db: LocalDatabase
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
        runner.run_storage.get_by_session.return_value = MagicMock(id="run-123")
        runner.complete_run.return_value = True
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

        response = await engine.evaluate(event, session_id="agent-session", variables=variables)

        instance = instance_manager.get_instance("agent-session", "plan-adversary-steps")
        assert instance is None
        assert variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert response.context is not None
        completion_registry.notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_qa_reviewer_stale_get_task_result_transitions_to_terminate(
        self, db: LocalDatabase
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
            session_id="agent-session",
            variables=variables,
        )

        instance = instance_manager.get_instance("agent-session", "qa-reviewer-steps")
        assert instance is not None
        assert instance.current_step == "terminate"
        assert instance.variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert response.context is not None
        assert "review -> terminate" in response.context

    @pytest.mark.asyncio
    async def test_parent_wait_unblocks_without_end_agent_run_tool_call(
        self, db: LocalDatabase
    ) -> None:
        _register_agent_workflow(db)
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(id="run-123")
        runner.complete_run.return_value = True
        completion_registry = CompletionEventRegistry()
        completion_registry.register("run-123", subscribers=[])

        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)

        # No end_agent_run tool call is issued in this test; workflow termination
        # must still wake the parent wait path immediately.
        await engine.evaluate(_after_tool_event(), session_id="agent-session", variables={})

        result = await completion_registry.wait("run-123", timeout=0.1)
        assert result["status"] == "success"
        assert result["via"] == "workflow_terminate"
        assert result["workflow"] == "plan-adversary-steps"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_workflow_termination_cleans_child_not_dispatcher_launcher(
        self, db: LocalDatabase
    ) -> None:
        db.execute(
            "INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (?, ?, datetime('now'))",
            ("project-1", "test-project"),
        )
        sessions = SessionManager(db)
        parent = sessions.register(
            external_id="dispatcher-launcher-project-1",
            machine_id="machine-1",
            source="dispatcher_launcher",
            project_id="project-1",
            title="Dispatcher Launcher",
        )
        child = sessions.register(
            external_id="child-session",
            machine_id="machine-1",
            source="codex",
            project_id="project-1",
            parent_session_id=parent.id,
            agent_depth=1,
        )
        task = LocalTaskManager(db).create_task(
            project_id="project-1",
            title="Workflow-owned task",
        )
        mutex = TaskDispatchMutexManager(db)
        mutex.acquire_mutex(
            task.id,
            holder="dispatcher",
            kind="spawn_agent",
            run_id="run-123",
            ttl_seconds=300,
        )
        _register_agent_workflow(db, session_id=child.id)

        run_manager = LocalAgentRunManager(db)
        run = run_manager.create(
            parent_session_id=parent.id,
            child_session_id=child.id,
            provider="codex",
            prompt="do work",
            run_id="run-123",
            task_id=task.id,
        )
        run_manager.start(run.id)
        completion_registry = CompletionEventRegistry()
        completion_registry.register(run.id, subscribers=[])
        runner = MagicMock()
        runner.run_storage = run_manager
        runner.agent_lifecycle_monitor = AgentLifecycleMonitor(
            agent_run_manager=run_manager,
            db=db,
            session_manager=sessions,
            completion_registry=completion_registry,
            task_manager=LocalTaskManager(db),
        )
        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)

        await engine.evaluate(
            _after_tool_event(session_id=child.id),
            session_id=child.id,
            variables={},
        )

        result = await completion_registry.wait(run.id, timeout=0.1)
        completed = run_manager.get(run.id)
        assert result == {
            "status": "success",
            "run_id": run.id,
            "via": "workflow_terminate",
            "workflow": "plan-adversary-steps",
        }
        assert completed is not None
        assert completed.status == "success"
        assert completed.parent_session_id == parent.id
        parent_lookup = sessions.get(parent.id)
        child_lookup = sessions.get(child.id)
        assert parent_lookup is not None
        assert child_lookup is not None
        assert parent_lookup.status == "active"
        assert child_lookup.status == "expired"
        assert mutex.get_mutex(task.id) is None
        assert WorkflowInstanceManager(db).get_active_instances(child.id) == []

    @pytest.mark.asyncio
    async def test_on_mcp_success_when_condition_checks_tool_argument(
        self, db: LocalDatabase
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
        runner.run_storage.get_by_session.return_value = MagicMock(id="run-123")
        runner.complete_run.return_value = True
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
            session_id="agent-session",
            variables=variables,
        )

        instance = instance_manager.get_instance("agent-session", "merge-orchestrator-test")
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
            session_id="agent-session",
            variables=variables,
        )

        instance = instance_manager.get_instance("agent-session", "merge-orchestrator-test")
        assert instance is None
        assert variables["review_complete"] is True
