"""Tests for step-level tool enforcement in the rule engine.

Tests WorkflowStep allowed_tools/blocked_tools/allowed_mcp_tools/blocked_mcp_tools
enforcement and step transitions via on_mcp_success handlers.
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition
from gobby.workflows.rule_engine import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_step_enforcement.db"
    database = LocalDatabase(db_path)
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
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata=metadata or {},
    )


# Developer workflow definition for tests
_DEVELOPER_WORKFLOW = {
    "name": "developer-workflow",
    "version": "2.0",
    "enabled": False,
    "variables": {"task_claimed": False, "review_submitted": False},
    "steps": [
        {
            "name": "claim",
            "allowed_tools": [
                "mcp__gobby__call_tool",
                "mcp__gobby__list_mcp_servers",
                "mcp__gobby__list_tools",
                "mcp__gobby__get_tool_schema",
            ],
            "allowed_mcp_tools": [
                "gobby-tasks:claim_task",
                "gobby-tasks:get_task",
            ],
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
        {
            "name": "implement",
            "allowed_tools": "all",
            "blocked_mcp_tools": [
                "gobby-tasks:close_task",
                "gobby-tasks:mark_task_review_approved",
                "gobby-agents:spawn_agent",
                "gobby-agents:kill_agent",
            ],
            "on_mcp_success": [
                {
                    "server": "gobby-tasks",
                    "tool": "mark_task_needs_review",
                    "action": "set_variable",
                    "variable": "review_submitted",
                    "value": True,
                }
            ],
            "transitions": [{"to": "terminate", "when": "vars.review_submitted"}],
        },
        {
            "name": "terminate",
            "allowed_tools": [
                "mcp__gobby__call_tool",
                "mcp__gobby__list_mcp_servers",
                "mcp__gobby__list_tools",
                "mcp__gobby__get_tool_schema",
            ],
            "allowed_mcp_tools": ["gobby-agents:kill_agent"],
        },
    ],
    "exit_condition": "current_step == 'terminate'",
}


def _create_session(db: LocalDatabase, session_id: str = "test-session") -> None:
    """Create a minimal session row to satisfy foreign key constraints."""
    db.execute(
        "INSERT OR IGNORE INTO projects (id, name, created_at) VALUES (?, ?, datetime('now'))",
        ("project-1", "test-project"),
    )
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        (session_id, "ext-1", "machine-1", "claude", "project-1"),
    )


def _setup_step_workflow(
    db: LocalDatabase,
    manager: LocalWorkflowDefinitionManager,
    instance_mgr: WorkflowInstanceManager,
    session_id: str = "test-session",
    current_step: str = "claim",
    workflow_data: dict[str, Any] | None = None,
) -> None:
    """Insert a workflow definition and create an active instance on a session."""
    _create_session(db, session_id)

    data = workflow_data or _DEVELOPER_WORKFLOW
    defn = WorkflowDefinition(**data)

    manager.create(
        name=defn.name,
        definition_json=json.dumps(data),
        workflow_type="workflow",
        priority=100,
        enabled=True,
    )

    from gobby.workflows.definitions import WorkflowInstance

    instance = WorkflowInstance(
        id=f"inst-{session_id}-{defn.name}",
        session_id=session_id,
        workflow_name=defn.name,
        enabled=True,
        priority=100,
        current_step=current_step,
        step_entered_at=datetime.now(UTC),
        variables=dict(defn.variables),
    )
    instance_mgr.save_instance(instance)


@pytest.mark.unit
class TestStepToolBlocking:
    """Test that step-level tool restrictions are enforced on BEFORE_TOOL events."""

    @pytest.mark.asyncio
    async def test_allowed_tool_passes(self, db, manager, engine, instance_mgr) -> None:
        """Tool in allowed_tools list should pass."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(data={"tool_name": "mcp__gobby__call_tool"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_disallowed_tool_blocked(self, db, manager, engine, instance_mgr) -> None:
        """Tool NOT in allowed_tools list should be blocked."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(data={"tool_name": "Edit"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "block"
        assert "step-enforcement" in response.reason
        assert "claim" in response.reason

    @pytest.mark.asyncio
    async def test_all_tools_allowed_when_set(self, db, manager, engine, instance_mgr) -> None:
        """When allowed_tools is 'all', any native tool should pass."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="implement")
        event = _make_event(data={"tool_name": "Edit"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_blocked_tools_enforced(self, db, manager, engine, instance_mgr) -> None:
        """Tool in blocked_tools list should be blocked even with allowed_tools='all'."""
        workflow = {
            "name": "test-blocked",
            "version": "2.0",
            "enabled": False,
            "steps": [
                {
                    "name": "work",
                    "allowed_tools": "all",
                    "blocked_tools": ["Write", "Edit"],
                }
            ],
        }
        _setup_step_workflow(db, manager, instance_mgr, current_step="work", workflow_data=workflow)
        event = _make_event(data={"tool_name": "Write"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "block"
        assert "blocked" in response.reason.lower()

    @pytest.mark.asyncio
    async def test_discovery_tools_always_pass(self, db, manager, engine, instance_mgr) -> None:
        """Discovery tools should pass regardless of step restrictions."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")

        for tool in [
            "mcp__gobby__list_mcp_servers",
            "mcp__gobby__list_tools",
            "mcp__gobby__get_tool_schema",
            "mcp__gobby__search_tools",
        ]:
            event = _make_event(data={"tool_name": tool})
            variables: dict[str, Any] = {}
            response = await engine.evaluate(event, session_id="test-session", variables=variables)
            assert response.decision == "allow", f"Discovery tool {tool} should pass"

    @pytest.mark.asyncio
    async def test_no_step_workflow_allows_all(self, db, manager, engine) -> None:
        """Without an active step workflow, all tools should pass."""
        event = _make_event(data={"tool_name": "Edit"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"


@pytest.mark.unit
class TestStepMCPToolBlocking:
    """Test MCP tool restrictions (allowed_mcp_tools/blocked_mcp_tools)."""

    @pytest.mark.asyncio
    async def test_allowed_mcp_tool_passes(self, db, manager, engine, instance_mgr) -> None:
        """MCP tool in allowed_mcp_tools should pass."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "claim_task",
                },
            }
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_disallowed_mcp_tool_blocked(self, db, manager, engine, instance_mgr) -> None:
        """MCP tool NOT in allowed_mcp_tools should be blocked."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                },
            }
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "block"
        assert "gobby-tasks:close_task" in response.reason

    @pytest.mark.asyncio
    async def test_blocked_mcp_tool_enforced(self, db, manager, engine, instance_mgr) -> None:
        """MCP tool in blocked_mcp_tools should be blocked."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="implement")
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                },
            }
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_mcp_discovery_tools_always_pass(self, db, manager, engine, instance_mgr) -> None:
        """MCP discovery tools should pass even when allowed_mcp_tools is restrictive."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "list_tools",
                },
            }
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_wildcard_mcp_tool_pattern(self, db, manager, engine, instance_mgr) -> None:
        """Wildcard pattern 'server:*' should match all tools on that server."""
        workflow = {
            "name": "test-wildcard",
            "version": "2.0",
            "enabled": False,
            "steps": [
                {
                    "name": "work",
                    "allowed_mcp_tools": ["gobby-merge:*"],
                }
            ],
        }
        _setup_step_workflow(db, manager, instance_mgr, current_step="work", workflow_data=workflow)
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-merge",
                    "tool_name": "merge_resolve",
                },
            }
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"


@pytest.mark.unit
class TestStepTransitions:
    """Test step transitions via on_mcp_success handlers."""

    @pytest.mark.asyncio
    async def test_on_mcp_success_sets_variable(self, db, manager, engine, instance_mgr) -> None:
        """on_mcp_success handler should set workflow instance variable."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "claim_task",
                },
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        # Check the instance was updated
        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        assert instance.variables.get("task_claimed") is True

    @pytest.mark.asyncio
    async def test_transition_fires_after_variable_set(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """Transition should fire when its condition becomes true via on_mcp_success."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "claim_task",
                },
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        assert instance.current_step == "implement"

    @pytest.mark.asyncio
    async def test_no_transition_on_failure(self, db, manager, engine, instance_mgr) -> None:
        """Failed tool calls should not trigger on_mcp_success or transitions."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "claim_task",
                },
                "is_error": True,
            },
            metadata={"is_failure": True},
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        assert instance.current_step == "claim"  # No transition

    @pytest.mark.asyncio
    async def test_implement_to_terminate_transition(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """mark_task_needs_review in implement step should transition to terminate."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="implement")
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "mark_task_needs_review",
                },
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        assert instance.current_step == "terminate"
        assert instance.variables.get("review_submitted") is True

    @pytest.mark.asyncio
    async def test_no_transition_for_unmatched_tool(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """MCP tools not in on_mcp_success should not trigger transitions."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="implement")
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "get_task",
                },
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        assert instance.current_step == "implement"  # No change


@pytest.mark.unit
class TestStepEnforcementAfterTransition:
    """Test that tool restrictions update after a step transition."""

    @pytest.mark.asyncio
    async def test_tools_restricted_after_transition_to_terminate(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """After transitioning to terminate, only kill_agent should be allowed."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="terminate")
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "claim_task",
                },
            }
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_kill_agent_allowed_in_terminate(self, db, manager, engine, instance_mgr) -> None:
        """kill_agent should be allowed in the terminate step."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="terminate")
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-agents",
                    "tool_name": "kill_agent",
                },
            }
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"
