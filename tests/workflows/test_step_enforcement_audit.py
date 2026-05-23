"""Audit logging regressions for step workflow enforcement."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition, WorkflowInstance
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager
from tests.fixtures.migrations import run_migrations

SESSION_ID = "audit-session"


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    database = LocalDatabase(tmp_path / "test_step_enforcement_audit.db")
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


@pytest.fixture
def engine(db: LocalDatabase) -> RuleEngine:
    return RuleEngine(db)


@pytest.fixture
def instance_mgr(db: LocalDatabase) -> WorkflowInstanceManager:
    return WorkflowInstanceManager(db)


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


def _create_session(db: LocalDatabase) -> None:
    db.execute(
        "INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        ("project-1", "test-project"),
    )
    db.execute(
        "INSERT OR IGNORE INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (SESSION_ID, "ext-1", "machine-1", "claude", "project-1"),
    )


def _setup_workflow(
    db: LocalDatabase,
    manager: LocalWorkflowDefinitionManager,
    instance_mgr: WorkflowInstanceManager,
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
    definition = WorkflowDefinition(**workflow_data)
    manager.create(
        name=definition.name,
        definition_json=json.dumps(workflow_data),
        workflow_type="workflow",
        priority=100,
        enabled=True,
    )
    instance_mgr.save_instance(
        WorkflowInstance(
            id=f"inst-{SESSION_ID}-{definition.name}",
            session_id=SESSION_ID,
            workflow_name=definition.name,
            enabled=True,
            priority=100,
            current_step="claim",
            step_entered_at=datetime.now(UTC),
            variables=dict(definition.variables),
        )
    )


def _audit_rows(db: LocalDatabase) -> list[dict[str, Any]]:
    rows = db.fetchall(
        """
        SELECT step, event_type, tool_name, condition, result, reason, context
        FROM workflow_audit_log
        WHERE session_id = ?
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
async def test_step_success_writes_audit_rows(db, manager, engine, instance_mgr) -> None:
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
async def test_step_mcp_block_writes_audit_row(db, manager, engine, instance_mgr) -> None:
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
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "tool_call"
    assert row["tool_name"] == "gobby-tasks:close_task"
    assert row["result"] == "block"
    assert row["reason"] == response.reason
    assert "MCP tool 'gobby-tasks:close_task' is not allowed" in row["reason"]
    assert row["context"]["workflow"] == "audit-workflow"
    assert row["context"]["step"] == "claim"
    assert row["context"]["mcp_key"] == "gobby-tasks:close_task"
