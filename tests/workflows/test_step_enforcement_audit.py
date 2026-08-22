"""Audit logging regressions for step workflow enforcement."""

import json
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import (
    AgentStepInstance,
    AgentStepInstanceManager,
    build_step_instance,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

# Session/project id columns are native uuid in PostgreSQL; synthetic ids
# would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


@pytest.fixture
def db(hub_db: "HubDatabase") -> "HubDatabase":
    return hub_db


@pytest.fixture
def manager(db: "HubDatabase") -> AgentDefinitionManager:
    return AgentDefinitionManager(db)


@pytest.fixture
def engine(db: "HubDatabase") -> RuleEngine:
    return RuleEngine(db)


@pytest.fixture
def instance_mgr(db: "HubDatabase") -> AgentStepInstanceManager:
    return AgentStepInstanceManager(db)


def _make_event(
    event_type: HookEventType,
    data: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata=metadata or {},
    )


def _create_session(db: "HubDatabase") -> None:
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (PROJECT_ID, "test-project"),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (SESSION_ID, "ext-1", "21000000-0000-4000-8000-000000000001", "claude", PROJECT_ID),
    )


def _setup_workflow(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    _create_session(db)
    workflow_data = {
        "name": "audit-workflow",
        "version": "1.0",
        "enabled": False,
        "variables": {"task_claimed": False},
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
                "transitions": [{"to": "implement", "when": "vars.task_claimed"}],
            },
            {"name": "implement", "allowed_tools": "all"},
        ],
        "exit_condition": "current_step == 'implement'",
    }
    definition = AgentDefinitionBody(
        prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
        name="audit-workflow",
        step_workflow=AgentStepWorkflowBody.model_validate(workflow_data),
    )
    manager.create(
        name=definition.name,
        definition_json=definition.model_dump(mode="json"),
        enabled=True,
    )
    instance_mgr.save(
        build_step_instance(
            definition,
            session_id=SESSION_ID,
            step_workflow_id=None,
            current_step="claim",
            variables=dict(definition.step_workflow.variables if definition.step_workflow else {}),
        )
    )


def _audit_rows(db: "HubDatabase") -> list[dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT step, event_type, tool_name, condition, result, reason, context
        FROM workflow_audit_log
        WHERE session_id = %s
        ORDER BY id
        """,
        (SESSION_ID,),
    )
    return [
        {
            **dict(row),
            "context": json.loads(row["context"]) if row["context"] else {},
        }
        for row in rows
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_step_success_writes_audit_rows(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    engine: RuleEngine,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    _setup_workflow(db, manager, instance_mgr)
    event = _make_event(
        HookEventType.AFTER_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
            },
            "tool_output": {"success": True, "result": {"task_id": "task-1"}},
        },
    )

    response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

    assert response.decision == "allow"
    rows = _audit_rows(db)
    by_type = {row["event_type"]: row for row in rows}

    assert by_type["tool_call"]["tool_name"] == "gobby-tasks:claim_task"
    assert by_type["tool_call"]["result"] == "allow"
    assert by_type["tool_call"]["context"]["workflow"] == "audit-workflow"

    assert by_type["set_variable"]["result"] == "set"
    assert by_type["set_variable"]["context"]["variable"] == "task_claimed"
    assert by_type["set_variable"]["context"]["value"] is True

    assert by_type["transition"]["result"] == "transition"
    assert by_type["transition"]["context"]["from_step"] == "claim"
    assert by_type["transition"]["context"]["to_step"] == "implement"
    assert by_type["transition"]["context"]["condition"] == "vars.task_claimed"
    assert by_type["transition"]["context"]["result"] is True

    assert by_type["exit_check"]["result"] == "met"
    assert by_type["exit_check"]["condition"] == "current_step == 'implement'"
    assert by_type["exit_check"]["context"]["result"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_step_transition_writes_run_outside_event_loop_thread(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    engine: RuleEngine,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    _setup_workflow(db, manager, instance_mgr)
    event = _make_event(
        HookEventType.AFTER_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "claim_task",
            },
            "tool_output": {"success": True, "result": {"task_id": "task-1"}},
        },
    )
    loop_thread_id = threading.get_ident()
    audit_threads: list[int] = []
    save_threads: list[int] = []
    original_log_transition = engine.workflow_audit.log_transition
    original_save = engine.instance_manager.save

    def log_transition(*args: object, **kwargs: object) -> None:
        audit_threads.append(threading.get_ident())
        original_log_transition(*args, **kwargs)

    def save(
        instance: AgentStepInstance,
        *,
        if_match: tuple[str, datetime] | None = None,
    ) -> None:
        save_threads.append(threading.get_ident())
        original_save(instance, if_match=if_match)

    with (
        patch.object(engine.workflow_audit, "log_transition", side_effect=log_transition),
        patch.object(engine.instance_manager, "save", side_effect=save),
    ):
        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

    assert response.decision == "allow"
    assert len(audit_threads) == 1
    assert len(save_threads) == 1
    assert audit_threads[0] != loop_thread_id
    assert save_threads[0] != loop_thread_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_step_mcp_block_writes_audit_row(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    engine: RuleEngine,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    _setup_workflow(db, manager, instance_mgr)
    event = _make_event(
        HookEventType.BEFORE_TOOL,
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
            },
        },
    )

    response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

    assert response.decision == "block"
    rows = _audit_rows(db)
    by_type = {row["event_type"]: row for row in rows}
    assert set(by_type) == {"rule_eval", "tool_call"}
    row = by_type["tool_call"]
    assert row["event_type"] == "tool_call"
    assert row["tool_name"] == "gobby-tasks:close_task"
    assert row["result"] == "block"
    assert row["reason"] == response.reason
    assert "MCP tool 'gobby-tasks:close_task' is not allowed" in row["reason"]
    assert row["context"]["workflow"] == "audit-workflow"
    assert row["context"]["step"] == "claim"
    assert row["context"]["mcp_key"] == "gobby-tasks:close_task"
