"""Tests for engine-side completion of agent-scoped step workflows."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
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
    review_tool: str = "mark_task_review_approved",
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
                        "server": "gobby-tasks",
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


def _after_tool_event(
    *,
    session_id: str = "agent-session",
    source: SessionSource = SessionSource.CLAUDE,
    mcp_server: str = "gobby-tasks",
    mcp_tool: str = "mark_task_review_approved",
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
    data = {
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
    async def test_exit_condition_completes_agent_run_and_notifies(self, db: LocalDatabase) -> None:
        _register_agent_workflow(db)
        runner = MagicMock()
        runner.run_storage = MagicMock()
        runner.run_storage.get_by_session.return_value = MagicMock(id="run-123")
        runner.complete_run.return_value = True
        completion_registry = MagicMock()
        completion_registry.get_result.return_value = None
        completion_registry.notify = AsyncMock()

        engine = RuleEngine(db, runner=runner, completion_registry=completion_registry)
        variables: dict[str, object] = {}

        await engine.evaluate(_after_tool_event(), session_id="agent-session", variables=variables)

        assert variables["step_workflow_complete"] is True
        runner.complete_run.assert_called_once_with("run-123", result=None)
        completion_registry.notify.assert_awaited_once_with(
            "run-123",
            {
                "status": "success",
                "run_id": "run-123",
                "via": "workflow_terminate",
                "workflow": "plan-adversary-steps",
            },
            message="Agent run-123 completed via workflow terminate",
        )

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
        completion_registry.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_codex_mcp_envelope_keeps_review_step_open(
        self, db: LocalDatabase
    ) -> None:
        instance_manager = _register_agent_workflow(
            db,
            review_tool="mark_task_review_rejected",
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
            mcp_tool="mark_task_review_rejected",
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
            mcp_tool="mark_task_review_rejected",
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
        assert instance is not None
        assert instance.current_step == "terminate"
        assert instance.variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert response.context is not None
        completion_registry.notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_closed_review_target_error_completes_plan_adversary_workflow(
        self, db: LocalDatabase
    ) -> None:
        instance_manager = _register_agent_workflow(
            db,
            review_tool="mark_task_review_rejected",
            review_error_handlers=[
                {
                    "server": "gobby-tasks",
                    "tool": "mark_task_review_rejected",
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
            mcp_tool="mark_task_review_rejected",
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
        assert instance is not None
        assert instance.current_step == "terminate"
        assert instance.variables["review_complete"] is True
        assert variables["step_workflow_complete"] is True
        assert response.context is not None
        completion_registry.notify.assert_awaited_once()

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
    async def test_on_mcp_success_when_can_require_final_tool_argument(
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
        assert instance is not None
        assert instance.current_step == "terminate"
        assert instance.variables["review_complete"] is True
        assert variables["review_complete"] is True
