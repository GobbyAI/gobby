"""Focused regression tests for step workflow runtime transitions."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


def _create_session(db: HubDatabase, session_id: str = "test-session") -> None:
    db.execute(
        """
        INSERT INTO projects (id, name, created_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        ("project-1", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (session_id, "ext-1", "machine-1", "claude", "project-1"),
    )


def _developer_workflow() -> dict[str, Any]:
    skill_gate = (
        "not vars.additional_skills "
        "or all(skill in vars.get('loaded_skills', []) for skill in vars.additional_skills)"
    )
    return {
        "name": "developer-steps",
        "version": "1.0",
        "enabled": True,
        "variables": {
            "task_claimed": False,
            "additional_skills": [],
            "additional_skills_loaded": False,
        },
        "steps": [
            {
                "name": "claim",
                "allowed_tools": ["mcp__gobby__call_tool"],
                "allowed_mcp_tools": ["gobby-tasks:claim_task"],
                "on_mcp_success": [
                    {
                        "server": "gobby-tasks",
                        "tool": "claim_task",
                        "action": "set_variable",
                        "variable": "task_claimed",
                        "value": True,
                    }
                ],
                "transitions": [{"to": "load_additional_skills", "when": "vars.task_claimed"}],
            },
            {
                "name": "load_additional_skills",
                "allowed_tools": ["mcp__gobby__call_tool"],
                "allowed_mcp_tools": ["gobby-skills:get_skill"],
                "on_mcp_success": [
                    {
                        "server": "gobby-skills",
                        "tool": "get_skill",
                        "action": "set_variable",
                        "variable": "additional_skills_loaded",
                        "value": skill_gate,
                    },
                ],
                "transitions": [{"to": "implement", "when": skill_gate}],
            },
            {"name": "implement", "allowed_tools": "all"},
        ],
    }


def _set_variable_workflow(step_variables: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": "set-variable-steps",
        "version": "1.0",
        "enabled": True,
        "variables": step_variables or {},
        "steps": [
            {
                "name": "plan",
                "allowed_tools": [
                    "mcp__gobby__set_variable",
                    "mcp__gobby__get_variable",
                ],
                "transitions": [{"to": "execute", "when": "vars.get('merge_plan')"}],
            },
            {"name": "execute", "allowed_tools": "all"},
        ],
    }


def _setup_workflow(
    db: HubDatabase,
    *,
    current_step: str = "claim",
    variables: dict[str, Any] | None = None,
    workflow: dict[str, Any] | None = None,
) -> WorkflowInstanceManager:
    _create_session(db)
    workflow = workflow or _developer_workflow()
    definition_manager = LocalWorkflowDefinitionManager(db)
    definition_manager.create(
        name=workflow["name"],
        definition_json=json.dumps(workflow),
        workflow_type="workflow",
        priority=100,
        enabled=True,
    )
    instance_manager = WorkflowInstanceManager(db)
    instance_manager.save_instance(
        WorkflowInstance(
            id="inst-test-session-developer",
            session_id="test-session",
            workflow_name=str(workflow["name"]),
            enabled=True,
            priority=100,
            current_step=current_step,
            step_entered_at=datetime.now(UTC),
            variables=variables if variables is not None else dict(workflow["variables"]),
        )
    )
    return instance_manager


def _after_mcp_tool(
    server: str,
    tool: str,
    *,
    arguments: dict[str, Any] | None = None,
) -> HookEvent:
    tool_input: dict[str, Any] = {"server_name": server, "tool_name": tool}
    if arguments is not None:
        tool_input["arguments"] = arguments
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": tool_input,
            "tool_output": {"success": True},
        },
        metadata={},
    )


def _before_set_variable(name: str, value: object) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__set_variable",
            "tool_input": {"name": name, "value": value},
        },
        metadata={},
    )


def _after_set_variable(
    name: str,
    value: object,
    *,
    output_value: object | None = None,
) -> HookEvent:
    tool_output: dict[str, Any] = {"success": True, "scope": "session"}
    if output_value is not None:
        tool_output["value"] = output_value
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__set_variable",
            "tool_input": {"name": name, "value": value, "session_id": "#1"},
            "tool_output": tool_output,
        },
        metadata={},
    )


def _before_mcp_set_variable(name: str, value: object) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-workflows",
                "tool_name": "set_variable",
                "arguments": {"name": name, "value": value},
            },
        },
        metadata={},
    )


@pytest.mark.asyncio
async def test_successful_claim_advances_through_empty_skill_gate(db: HubDatabase) -> None:
    instance_manager = _setup_workflow(db)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _after_mcp_tool("gobby-tasks", "claim_task"),
        session_id="test-session",
        variables=variables,
    )

    instance = instance_manager.get_instance("test-session", "developer-steps")
    assert instance is not None
    assert instance.current_step == "implement"
    assert response.context is not None
    assert "claim" in response.context
    assert "load_additional_skills" in response.context
    assert "implement" in response.context


@pytest.mark.asyncio
async def test_required_additional_skills_gate_exact_loaded_skill_names(
    db: HubDatabase,
) -> None:
    instance_manager = _setup_workflow(
        db,
        current_step="load_additional_skills",
        variables={
            "task_claimed": True,
            "additional_skills": ["code-index"],
            "additional_skills_loaded": False,
        },
    )
    engine = RuleEngine(db)
    variables: dict[str, Any] = {"loaded_skills": ["python"]}

    await engine.evaluate(
        _after_mcp_tool("gobby-skills", "get_skill", arguments={"name": "python"}),
        session_id="test-session",
        variables=variables,
    )
    instance = instance_manager.get_instance("test-session", "developer-steps")
    assert instance is not None
    assert instance.current_step == "load_additional_skills"
    assert instance.variables["additional_skills_loaded"] is False

    variables["loaded_skills"] = ["python", "code-index"]
    await engine.evaluate(
        _after_mcp_tool("gobby-skills", "get_skill", arguments={"name": "code-index"}),
        session_id="test-session",
        variables=variables,
    )
    instance = instance_manager.get_instance("test-session", "developer-steps")
    assert instance is not None
    assert instance.current_step == "implement"
    assert instance.variables["additional_skills_loaded"] is True


@pytest.mark.asyncio
async def test_step_workflow_complete_user_write_is_blocked(db: HubDatabase) -> None:
    _setup_workflow(db, current_step="implement")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _before_set_variable("step_workflow_complete", True),
        session_id="test-session",
        variables=variables,
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "step_workflow_complete" in response.reason


@pytest.mark.asyncio
async def test_step_workflow_complete_call_tool_write_is_blocked(db: HubDatabase) -> None:
    _setup_workflow(db, current_step="implement")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _before_mcp_set_variable("step_workflow_complete", True),
        session_id="test-session",
        variables=variables,
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "step_workflow_complete" in response.reason


@pytest.mark.asyncio
async def test_non_reserved_set_variable_remains_allowed(db: HubDatabase) -> None:
    _setup_workflow(db, current_step="implement")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _before_set_variable("lint_passed", True),
        session_id="test-session",
        variables=variables,
    )

    assert response.decision == "allow"


@pytest.mark.asyncio
async def test_missing_session_scoped_transition_variable_waits_without_error(
    db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workflow = _set_variable_workflow()
    instance_manager = _setup_workflow(db, current_step="plan", workflow=workflow)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}
    caplog.set_level(logging.ERROR, logger="gobby.workflows.engine.templating")

    response = await engine.evaluate(
        _before_set_variable("lint_passed", True),
        session_id="test-session",
        variables=variables,
    )

    instance = instance_manager.get_instance("test-session", "set-variable-steps")
    assert instance is not None
    assert instance.current_step == "plan"
    assert response.decision == "allow"
    assert "Failed to evaluate condition" not in caplog.text


@pytest.mark.asyncio
async def test_native_set_variable_advances_session_scoped_transition(
    db: HubDatabase,
) -> None:
    workflow = _set_variable_workflow()
    instance_manager = _setup_workflow(db, current_step="plan", workflow=workflow)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _after_set_variable("merge_plan", {"steps": ["leaf"]}),
        session_id="test-session",
        variables=variables,
    )

    instance = instance_manager.get_instance("test-session", "set-variable-steps")
    assert instance is not None
    assert instance.current_step == "execute"
    assert variables["merge_plan"] == {"steps": ["leaf"]}
    assert response.context is not None
    assert "plan" in response.context
    assert "execute" in response.context


@pytest.mark.asyncio
async def test_native_set_variable_does_not_shadow_workflow_local_variable(
    db: HubDatabase,
) -> None:
    workflow = _set_variable_workflow({"merge_plan": False})
    instance_manager = _setup_workflow(
        db,
        current_step="plan",
        variables={"merge_plan": False},
        workflow=workflow,
    )
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    await engine.evaluate(
        _after_set_variable("merge_plan", {"steps": ["leaf"]}),
        session_id="test-session",
        variables=variables,
    )

    instance = instance_manager.get_instance("test-session", "set-variable-steps")
    assert instance is not None
    assert instance.current_step == "plan"
    assert instance.variables["merge_plan"] is False
    assert "merge_plan" not in variables
