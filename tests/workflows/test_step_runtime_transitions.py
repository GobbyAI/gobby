"""Focused regression tests for step workflow runtime transitions."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager

pytestmark = pytest.mark.unit


async def test_completion_normalization_runs_off_loop(db: HubDatabase) -> None:
    import threading
    from unittest.mock import patch

    from gobby.hooks.normalization import normalize_tool_fields
    from gobby.hooks.tool_outcomes import tool_outcome_from_data

    _setup_workflow(db)
    loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def normalize(data: dict[str, Any]) -> None:
        worker_threads.append(threading.get_ident())
        normalize_tool_fields(data)

    event = _after_mcp_tool("gobby-tasks", "claim_task", arguments={"task_id": "task"})
    with patch(
        "gobby.workflows.engine.enforcement_completion.normalize_tool_fields",
        side_effect=normalize,
    ):
        await RuleEngine(db)._process_step_after_tool(event, SESSION_ID, {})
    assert worker_threads and all(worker != loop_thread for worker in worker_threads)
    assert tool_outcome_from_data(event.data).succeeded is True


# Session/project/instance id columns are native uuid in PostgreSQL; synthetic
# ids like SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


def _create_session(db: HubDatabase, session_id: str = SESSION_ID) -> None:
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


# Handler conditions see the inner MCP arguments, so the task id is a flat key.
_ASSIGNED_TASK_CALL = "tool_input.get('task_id') in (vars.assigned_task_id, vars.assigned_task_ref)"


def _task_state_reset() -> dict[str, Any]:
    """The per-task state a developer seat clears when it claims a task or hands one back."""
    return {
        "task_claimed": False,
        "implementation_complete": False,
        "candidate_sent": False,
        "skills_routed": False,
        "additional_skills_loaded": False,
        "bounced": False,
        "assigned_task_ref": None,
        "additional_skills": [],
    }


def _reset_handlers(server: str, tool: str, when: str | None = None) -> list[dict[str, Any]]:
    handlers: list[dict[str, Any]] = []
    for name, value in _task_state_reset().items():
        handler: dict[str, Any] = {
            "server": server,
            "tool": tool,
            "action": "set_variable",
            "variable": name,
            "value": value,
        }
        if when is not None:
            handler["when"] = when
        handlers.append(handler)
    return handlers


def _handoff_workflow() -> dict[str, Any]:
    """A developer-shaped loop: a claim binds its task, and handing the assigned task back resets."""
    bind_claim = {
        "server": "gobby-tasks",
        "tool": "claim_task",
        "action": "set_variable",
        "variable": "assigned_task_id",
        "value": "(tool_output.get('result') or tool_output).get('task_id')",
    }
    bind_ref = {
        "server": "gobby-tasks",
        "tool": "get_task",
        "action": "set_variable",
        "variable": "assigned_task_ref",
        "value": "(tool_output.get('result') or tool_output).get('ref')",
    }
    link_commit = {
        "server": "gobby-tasks",
        "tool": "link_commit",
        "action": "set_variable",
        "variable": "implementation_complete",
        "value": True,
    }
    # Latch the match first: the resets rewrite the binding the match reads.
    latch_handed_back = {
        "server": "gobby-tasks",
        "tool": "escalate_task",
        "action": "set_variable",
        "variable": "handed_back",
        "value": True,
        "when": f"tool_output.get('success') and {_ASSIGNED_TASK_CALL}",
    }
    clear_assigned_task = {
        "server": "gobby-tasks",
        "tool": "escalate_task",
        "action": "set_variable",
        "variable": "assigned_task_id",
        "value": None,
        "when": "vars.handed_back",
    }
    unlatch_handed_back = {
        "server": "gobby-tasks",
        "tool": "escalate_task",
        "action": "set_variable",
        "variable": "handed_back",
        "value": False,
    }
    return {
        "name": "handoff-steps",
        "version": "1.0",
        "enabled": True,
        "variables": {
            "task_claimed": False,
            "assigned_task_id": None,
            "assigned_task_ref": None,
            "implementation_complete": False,
            "candidate_sent": False,
            "skills_routed": False,
            "additional_skills": [],
            "additional_skills_loaded": False,
            "bounced": False,
            "handed_back": False,
        },
        "steps": [
            {
                "name": "claim",
                "on_mcp_success": [
                    bind_claim,
                    *_reset_handlers("gobby-tasks", "claim_task"),
                    {
                        "server": "gobby-tasks",
                        "tool": "claim_task",
                        "action": "set_variable",
                        "variable": "task_claimed",
                        "value": True,
                    },
                ],
                "transitions": [{"to": "route_skills", "when": "vars.assigned_task_id"}],
            },
            {
                "name": "route_skills",
                "on_mcp_success": [bind_ref],
                "transitions": [{"to": "implement", "when": "vars.skills_routed"}],
            },
            {
                "name": "implement",
                "on_mcp_success": [link_commit],
                "transitions": [{"to": "submit", "when": "vars.implementation_complete"}],
            },
            {
                "name": "submit",
                "on_mcp_success": [
                    latch_handed_back,
                    *_reset_handlers("gobby-tasks", "escalate_task", "vars.handed_back"),
                    clear_assigned_task,
                    unlatch_handed_back,
                ],
                "transitions": [{"to": "claim", "when": "not vars.assigned_task_id"}],
            },
        ],
    }


def _required_skills_workflow() -> dict[str, Any]:
    """A load_skills step that holds until every required skill is in the loaded ledger."""
    loaded = "all(skill in vars.get('loaded_skills', []) for skill in vars.required_skills)"
    return {
        "name": "required-skills-steps",
        "version": "1.0",
        "enabled": True,
        "variables": {
            "task_claimed": False,
            "required_skills": [],
            "required_skills_loaded": False,
        },
        "steps": [
            {
                "name": "load_skills",
                "allowed_tools": ["mcp__gobby__call_tool"],
                "allowed_mcp_tools": ["gobby-skills:get_skill"],
                "on_mcp_success": [
                    {
                        "server": "gobby-skills",
                        "tool": "get_skill",
                        "action": "set_variable",
                        "variable": "required_skills_loaded",
                        "value": loaded,
                    }
                ],
                "transitions": [{"to": "claim", "when": "vars.required_skills_loaded"}],
            },
            {"name": "claim", "allowed_tools": "all"},
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
) -> AgentStepInstanceManager:
    _create_session(db)
    workflow = workflow or _developer_workflow()
    definition_manager = AgentDefinitionManager(db)
    definition_manager.create(
        name=workflow["name"],
        definition_json=json.dumps(workflow),
        enabled=True,
    )
    instance_manager = AgentStepInstanceManager(db)
    instance_manager.save(
        AgentStepInstance(
            id=str(uuid.uuid4()),
            session_id=SESSION_ID,
            agent_name=str(workflow["name"]),
            snapshot=AgentStepWorkflowBody.model_validate(
                {
                    "steps": workflow["steps"],
                    "variables": workflow.get("variables") or {},
                    "exit_condition": workflow.get("exit_condition"),
                }
            ),
            enabled=True,
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
    output: dict[str, Any] | None = None,
) -> HookEvent:
    tool_input: dict[str, Any] = {"server_name": server, "tool_name": tool}
    if arguments is not None:
        tool_input["arguments"] = arguments
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": tool_input,
            "tool_output": output if output is not None else {"success": True},
        },
        metadata={},
    )


def _before_set_variable(name: str, value: object) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
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
        session_id=SESSION_ID,
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
        session_id=SESSION_ID,
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


@pytest.mark.parametrize("task_ref", ["old-task", "#101"])
async def test_handoff_of_assigned_task_resets_before_next_task(
    db: HubDatabase, task_ref: str
) -> None:
    workflow = _handoff_workflow()
    stale = {
        **workflow["variables"],
        "task_claimed": True,
        "assigned_task_id": "old-task",
        "assigned_task_ref": "#101",
        "implementation_complete": True,
        "candidate_sent": True,
        "skills_routed": True,
        "additional_skills_loaded": True,
        "additional_skills": ["python"],
    }
    manager = _setup_workflow(db, current_step="submit", variables=stale, workflow=workflow)
    engine = RuleEngine(db)
    await engine._process_step_after_tool(
        _after_mcp_tool("gobby-tasks", "escalate_task", arguments={"task_id": task_ref}),
        SESSION_ID,
        {},
    )
    instance = manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "claim"
    for flag in (
        "task_claimed",
        "implementation_complete",
        "candidate_sent",
        "skills_routed",
        "additional_skills_loaded",
        "bounced",
    ):
        assert instance.variables[flag] is False
    assert instance.variables["assigned_task_id"] is None
    assert instance.variables["assigned_task_ref"] is None

    await engine._process_step_after_tool(
        _after_mcp_tool(
            "gobby-tasks",
            "claim_task",
            arguments={"task_id": "new-task"},
            output={"success": True, "result": {"task_id": "new-task"}},
        ),
        SESSION_ID,
        {},
    )
    instance = manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "route_skills"
    await engine._process_step_after_tool(
        _after_mcp_tool(
            "gobby-tasks",
            "get_task",
            arguments={"task_id": "new-task"},
            output={"success": True, "result": {"id": "new-task", "ref": "#102"}},
        ),
        SESSION_ID,
        {},
    )
    instance = manager.get_for_session(SESSION_ID)
    assert instance is not None
    instance.variables.update(skills_routed=True, additional_skills=[])
    manager.save(instance)
    await engine._process_step_after_tool(
        _after_set_variable("skills_routed", True),
        SESSION_ID,
        {},
    )
    instance = manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "implement"
    assert instance.variables["implementation_complete"] is False
    await engine._process_step_after_tool(
        _after_mcp_tool("gobby-tasks", "link_commit", arguments={"task_id": "new-task"}),
        SESSION_ID,
        {},
    )
    instance = manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "submit"


@pytest.mark.parametrize(
    ("task_ref", "success"),
    [("foreign-task", True), ("old-task", False)],
)
async def test_unsuccessful_or_foreign_handoff_keeps_binding(
    db: HubDatabase,
    task_ref: str,
    success: bool,
) -> None:
    workflow = _handoff_workflow()
    variables = {
        **workflow["variables"],
        "task_claimed": True,
        "assigned_task_id": "old-task",
        "assigned_task_ref": "#101",
        "implementation_complete": True,
        "candidate_sent": True,
    }
    manager = _setup_workflow(db, current_step="submit", variables=variables, workflow=workflow)
    await RuleEngine(db)._process_step_after_tool(
        _after_mcp_tool(
            "gobby-tasks",
            "escalate_task",
            arguments={"task_id": task_ref},
            output={"success": success},
        ),
        SESSION_ID,
        {},
    )
    instance = manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "submit"
    assert instance.variables == variables


async def test_claim_discards_stale_completion(db: HubDatabase) -> None:
    workflow = _handoff_workflow()
    variables = {
        **workflow["variables"],
        "assigned_task_ref": "#101",
        "implementation_complete": True,
        "candidate_sent": True,
        "skills_routed": True,
        "additional_skills_loaded": True,
        "additional_skills": ["python"],
        "bounced": True,
    }
    manager = _setup_workflow(db, variables=variables, workflow=workflow)
    await RuleEngine(db)._process_step_after_tool(
        _after_mcp_tool(
            "gobby-tasks",
            "claim_task",
            arguments={"task_id": "new-task"},
            output={"success": True, "result": {"task_id": "new-task"}},
        ),
        SESSION_ID,
        {},
    )
    instance = manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "route_skills"
    assert instance.variables["assigned_task_id"] == "new-task"
    assert instance.variables["assigned_task_ref"] is None
    assert instance.variables["additional_skills"] == []
    for flag in (
        "implementation_complete",
        "candidate_sent",
        "skills_routed",
        "additional_skills_loaded",
        "bounced",
    ):
        assert instance.variables[flag] is False


@pytest.mark.asyncio
async def test_successful_claim_advances_through_empty_skill_gate(db: HubDatabase) -> None:
    instance_manager = _setup_workflow(db)
    engine = RuleEngine(db)
    variables: dict[str, Any] = {}

    response = await engine.evaluate(
        _after_mcp_tool("gobby-tasks", "claim_task"),
        session_id=SESSION_ID,
        variables=variables,
    )

    instance = instance_manager.get_for_session(SESSION_ID)
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
        session_id=SESSION_ID,
        variables=variables,
    )
    instance = instance_manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "load_additional_skills"
    assert instance.variables["additional_skills_loaded"] is False

    variables["loaded_skills"] = ["python", "code-index"]
    await engine.evaluate(
        _after_mcp_tool("gobby-skills", "get_skill", arguments={"name": "code-index"}),
        session_id=SESSION_ID,
        variables=variables,
    )
    instance = instance_manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "implement"
    assert instance.variables["additional_skills_loaded"] is True


async def test_required_skills_gate_holds_until_skill_is_loaded(db: HubDatabase) -> None:
    workflow = _required_skills_workflow()
    step_variables = dict(workflow["variables"])
    step_variables["required_skills"] = ["restraint"]
    instance_manager = _setup_workflow(
        db,
        current_step="load_skills",
        variables=step_variables,
        workflow=workflow,
    )
    engine = RuleEngine(db)
    variables: dict[str, Any] = {
        "loaded_skills": ["unrelated"],
        "loaded_skill_references": [],
    }

    await engine.evaluate(
        _after_mcp_tool("gobby-skills", "get_skill", arguments={"name": "unrelated"}),
        session_id=SESSION_ID,
        variables=variables,
    )
    instance = instance_manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "load_skills"
    assert instance.variables["required_skills_loaded"] is False

    variables["loaded_skills"].append("restraint")
    await engine.evaluate(
        _after_mcp_tool("gobby-skills", "get_skill", arguments={"name": "restraint"}),
        session_id=SESSION_ID,
        variables=variables,
    )
    instance = instance_manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "claim"
    assert instance.variables["required_skills_loaded"] is True


def test_step_handler_expression_failure_logs_and_fails_closed(
    db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = RuleEngine(db)

    with caplog.at_level(logging.WARNING, logger="gobby.workflows.engine.enforcement"):
        result = engine._evaluate_step_handler_value(
            "vars.value + missing_function()",
            {"vars": {"value": 1}},
            "set_variable",
        )

    assert result == (False, None)
    assert "Failed to evaluate step set_variable handler value" in caplog.text
    assert "Function not allowed: missing_function" in caplog.text


@pytest.mark.asyncio
async def test_step_workflow_complete_user_write_is_blocked(db: HubDatabase) -> None:
    _setup_workflow(db, current_step="implement")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {"_agent_type": "default"}

    response = await engine.evaluate(
        _before_set_variable("step_workflow_complete", True),
        session_id=SESSION_ID,
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
        session_id=SESSION_ID,
        variables=variables,
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "step_workflow_complete" in response.reason


@pytest.mark.parametrize(
    "name",
    [
        "enforce_tool_schema_check",
        "unlocked_tools",
        "listed_servers",
        "consecutive_tool_blocks",
        "_last_blocked_tool",
        "edit_write_pending",
    ],
)
@pytest.mark.asyncio
async def test_runtime_managed_write_is_blocked_without_step_workflow(
    db: HubDatabase, name: str
) -> None:
    _create_session(db)
    response = await RuleEngine(db).evaluate(
        _before_set_variable(name, True),
        session_id=SESSION_ID,
        variables={},
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert name in response.reason


@pytest.mark.asyncio
async def test_runtime_managed_call_tool_write_is_blocked_without_step_workflow(
    db: HubDatabase,
) -> None:
    response = await RuleEngine(db).evaluate(
        _before_mcp_set_variable("unlocked_tools", ["gobby-tasks:delete_task"]),
        session_id=SESSION_ID,
        variables={},
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "unlocked_tools" in response.reason


@pytest.mark.asyncio
async def test_non_reserved_set_variable_remains_allowed(db: HubDatabase) -> None:
    _setup_workflow(db, current_step="implement")
    engine = RuleEngine(db)
    variables: dict[str, Any] = {"_agent_type": "default"}

    response = await engine.evaluate(
        _before_set_variable("lint_passed", True),
        session_id=SESSION_ID,
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
    variables: dict[str, Any] = {"_agent_type": "default"}
    caplog.set_level(logging.ERROR, logger="gobby.workflows.engine.templating")

    response = await engine.evaluate(
        _before_set_variable("lint_passed", True),
        session_id=SESSION_ID,
        variables=variables,
    )

    instance = instance_manager.get_for_session(SESSION_ID)
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
        session_id=SESSION_ID,
        variables=variables,
    )

    instance = instance_manager.get_for_session(SESSION_ID)
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
        session_id=SESSION_ID,
        variables=variables,
    )

    instance = instance_manager.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "plan"
    assert instance.variables["merge_plan"] is False
    assert "merge_plan" not in variables
