"""Step workflow routing tests for structured MCP error codes."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager

pytestmark = pytest.mark.unit

# Session/project id columns are native uuid in PostgreSQL; synthetic ids
# like SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> AgentDefinitionManager:
    return AgentDefinitionManager(db)


@pytest.fixture
def engine(db: HubDatabase) -> RuleEngine:
    return RuleEngine(db)


@pytest.fixture
def instance_mgr(db: HubDatabase) -> AgentStepInstanceManager:
    return AgentStepInstanceManager(db)


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


def _setup_workflow(
    db: HubDatabase,
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
    workflow_data: dict[str, Any],
    *,
    current_step: str,
    session_id: str = SESSION_ID,
) -> None:
    _create_session(db, session_id)
    definition = WorkflowDefinition(**workflow_data)
    manager.create(
        name=definition.name,
        definition_json=json.dumps(workflow_data),
        enabled=True,
    )
    instance_mgr.save(
        AgentStepInstance(
            id=str(uuid.uuid4()),
            session_id=session_id,
            agent_name=definition.name,
            snapshot=AgentStepWorkflowBody(steps=[WorkflowStep(name=current_step)]),
            enabled=True,
            current_step=current_step,
            step_entered_at=datetime.now(UTC),
            variables=dict(definition.variables),
        )
    )


def _after_call_tool_event(tool_output: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
            },
            "tool_output": tool_output,
        },
        metadata={},
    )


@pytest.mark.asyncio
async def test_on_mcp_error_when_branches_on_error_code(
    db: HubDatabase,
    manager: AgentDefinitionManager,
    engine: RuleEngine,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    workflow = {
        "name": "task-error-code-workflow",
        "version": "1.0",
        "enabled": False,
        "variables": {"task_claimed": False},
        "steps": [
            {
                "name": "claim",
                "allowed_tools": "all",
                "on_mcp_error": [
                    {
                        "server": "gobby-tasks",
                        "tool": "claim_task",
                        "when": 'tool_output.error_code == "TASK_CLOSED"',
                        "action": "set_variable",
                        "variable": "task_claimed",
                        "value": True,
                    }
                ],
                "transitions": [{"to": "done", "when": "vars.task_claimed"}],
            },
            {"name": "done", "allowed_tools": "all"},
        ],
    }
    _setup_workflow(db, manager, instance_mgr, workflow, current_step="claim")

    await engine.evaluate(
        _after_call_tool_event(
            {
                "success": False,
                "status": "error",
                "error": "Cannot claim task #1: task is closed",
                "error_code": "TASK_CLOSED",
            }
        ),
        session_id=SESSION_ID,
        variables={},
    )

    instance = instance_mgr.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.variables.get("task_claimed") is True
    assert instance.current_step == "done"
