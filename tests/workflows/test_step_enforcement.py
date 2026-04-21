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

        response = await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        assert instance.current_step == "implement"
        # Transition notification should be in the response context
        assert response.context is not None
        assert "claim" in response.context
        assert "implement" in response.context

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

        response = await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        assert instance.current_step == "terminate"
        assert instance.variables.get("review_submitted") is True
        # Transition notification should be in the response context
        assert response.context is not None
        assert "implement" in response.context
        assert "terminate" in response.context

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

    @pytest.mark.asyncio
    async def test_no_transition_returns_no_context(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """When no transition fires, response context should not contain transition info."""
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

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        # No transition means no transition context
        assert response.context is None or "Step transition" not in response.context

    @pytest.mark.asyncio
    async def test_transition_includes_status_message(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """Transition notification should include the new step's status_message."""
        workflow_with_status = {
            "name": "status-msg-workflow",
            "version": "2.0",
            "enabled": False,
            "variables": {"done": False},
            "steps": [
                {
                    "name": "working",
                    "allowed_tools": "all",
                    "on_mcp_success": [
                        {
                            "server": "gobby-tasks",
                            "tool": "mark_task_needs_review",
                            "action": "set_variable",
                            "variable": "done",
                            "value": True,
                        }
                    ],
                    "transitions": [{"to": "finished", "when": "vars.done"}],
                },
                {
                    "name": "finished",
                    "status_message": "Call kill_agent to terminate.",
                    "allowed_tools": ["mcp__gobby__call_tool"],
                    "allowed_mcp_tools": ["gobby-agents:kill_agent"],
                },
            ],
        }
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="working", workflow_data=workflow_with_status
        )
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

        response = await engine.evaluate(event, session_id="test-session", variables=variables)

        assert response.context is not None
        assert "working" in response.context
        assert "finished" in response.context
        assert "Call kill_agent to terminate." in response.context

    @pytest.mark.asyncio
    async def test_on_mcp_success_handler_when_gates_variable_update(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """Handler-level when clauses should gate on_mcp_success variable updates."""
        workflow = {
            "name": "skill-gate-workflow",
            "version": "1.0",
            "enabled": False,
            "variables": {"skill_loaded": False},
            "steps": [
                {
                    "name": "load",
                    "allowed_tools": "all",
                    "on_mcp_success": [
                        {
                            "server": "gobby-skills",
                            "tool": "get_skill",
                            "when": "tool_input.name == 'plan-draft'",
                            "action": "set_variable",
                            "variable": "skill_loaded",
                            "value": True,
                        }
                    ],
                }
            ],
        }
        _setup_step_workflow(db, manager, instance_mgr, current_step="load", workflow_data=workflow)

        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-skills",
                    "tool_name": "get_skill",
                    "arguments": {"name": "plan-review"},
                },
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "skill-gate-workflow")
        assert instance is not None
        assert instance.variables.get("skill_loaded") is False

        matching_event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-skills",
                    "tool_name": "get_skill",
                    "arguments": {"name": "plan-draft"},
                },
            },
        )

        await engine.evaluate(matching_event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "skill-gate-workflow")
        assert instance is not None
        assert instance.variables.get("skill_loaded") is True


# Workflow with on_mcp_error handlers for testing app-level failure routing
_MERGE_WORKFLOW = {
    "name": "merge-workflow",
    "version": "1.0",
    "enabled": False,
    "variables": {"merge_complete": False, "has_conflicts": False},
    "steps": [
        {
            "name": "merge",
            "allowed_tools": "all",
            "on_mcp_success": [
                {
                    "server": "gobby-worktrees",
                    "tool": "merge_worktree",
                    "action": "set_variable",
                    "variable": "merge_complete",
                    "value": True,
                }
            ],
            "on_mcp_error": [
                {
                    "server": "gobby-worktrees",
                    "tool": "merge_worktree",
                    "action": "set_variable",
                    "variable": "has_conflicts",
                    "value": True,
                }
            ],
            "transitions": [
                {"to": "resolve_conflicts", "when": "vars.has_conflicts"},
                {"to": "done", "when": "vars.merge_complete"},
            ],
        },
        {"name": "resolve_conflicts", "allowed_tools": "all"},
        {"name": "done", "allowed_tools": "all"},
    ],
}


@pytest.mark.unit
class TestToolOutputRouting:
    """Test that on_mcp_success vs on_mcp_error routes based on tool_output.success."""

    @pytest.mark.asyncio
    async def test_on_mcp_success_skipped_on_tool_failure(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """Tool output with success:false should NOT fire on_mcp_success handlers."""
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="merge", workflow_data=_MERGE_WORKFLOW
        )
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-worktrees",
                    "tool_name": "merge_worktree",
                },
                "tool_output": {"success": False, "has_conflicts": True},
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "merge-workflow")
        assert instance is not None
        # merge_complete should NOT be set (on_mcp_success was skipped)
        assert instance.variables.get("merge_complete") is False

    @pytest.mark.asyncio
    async def test_on_mcp_error_fires_on_tool_failure(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """Tool output with success:false should fire on_mcp_error handlers."""
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="merge", workflow_data=_MERGE_WORKFLOW
        )
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-worktrees",
                    "tool_name": "merge_worktree",
                },
                "tool_output": {"success": False, "has_conflicts": True},
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "merge-workflow")
        assert instance is not None
        assert instance.variables.get("has_conflicts") is True
        # Transition to resolve_conflicts should fire
        assert instance.current_step == "resolve_conflicts"

    @pytest.mark.asyncio
    async def test_on_mcp_success_fires_on_tool_success(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """Tool output with success:true should fire on_mcp_success (no regression)."""
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="merge", workflow_data=_MERGE_WORKFLOW
        )
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-worktrees",
                    "tool_name": "merge_worktree",
                },
                "tool_output": {"success": True, "message": "Merged successfully"},
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "merge-workflow")
        assert instance is not None
        assert instance.variables.get("merge_complete") is True
        assert instance.variables.get("has_conflicts") is False
        assert instance.current_step == "done"

    @pytest.mark.asyncio
    async def test_on_mcp_error_with_nested_result(self, db, manager, engine, instance_mgr) -> None:
        """Proxy-wrapped response {success:true, result:{success:false}} should route to on_mcp_error."""
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="merge", workflow_data=_MERGE_WORKFLOW
        )
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-worktrees",
                    "tool_name": "merge_worktree",
                },
                "tool_output": {
                    "success": True,
                    "result": {"success": False, "has_conflicts": True},
                },
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "merge-workflow")
        assert instance is not None
        assert instance.variables.get("has_conflicts") is True
        assert instance.current_step == "resolve_conflicts"

    @pytest.mark.asyncio
    async def test_no_tool_output_uses_on_mcp_success(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """When tool_output is absent, should default to on_mcp_success (backward compat)."""
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="merge", workflow_data=_MERGE_WORKFLOW
        )
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-worktrees",
                    "tool_name": "merge_worktree",
                },
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "merge-workflow")
        assert instance is not None
        # Without tool_output, should fall through to on_mcp_success
        assert instance.variables.get("merge_complete") is True
        assert instance.current_step == "done"

    @pytest.mark.asyncio
    async def test_string_tool_output_parsed(self, db, manager, engine, instance_mgr) -> None:
        """JSON string tool_output should be parsed and routed correctly."""
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="merge", workflow_data=_MERGE_WORKFLOW
        )
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-worktrees",
                    "tool_name": "merge_worktree",
                },
                "tool_output": '{"success": false, "has_conflicts": true}',
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "merge-workflow")
        assert instance is not None
        assert instance.variables.get("has_conflicts") is True
        assert instance.current_step == "resolve_conflicts"

    @pytest.mark.asyncio
    async def test_on_mcp_error_handler_when_gates_variable_update(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """Handler-level when clauses should gate on_mcp_error variable updates."""
        workflow = {
            "name": "merge-when-workflow",
            "version": "1.0",
            "enabled": False,
            "variables": {"has_conflicts": False},
            "steps": [
                {
                    "name": "merge",
                    "allowed_tools": "all",
                    "on_mcp_error": [
                        {
                            "server": "gobby-worktrees",
                            "tool": "merge_worktree",
                            "when": "tool_output.result.has_conflicts",
                            "action": "set_variable",
                            "variable": "has_conflicts",
                            "value": True,
                        }
                    ],
                }
            ],
        }
        _setup_step_workflow(db, manager, instance_mgr, current_step="merge", workflow_data=workflow)

        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-worktrees",
                    "tool_name": "merge_worktree",
                },
                "tool_output": {
                    "success": True,
                    "result": {"success": False, "has_conflicts": False},
                },
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "merge-when-workflow")
        assert instance is not None
        assert instance.variables.get("has_conflicts") is False

        matching_event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-worktrees",
                    "tool_name": "merge_worktree",
                },
                "tool_output": {
                    "success": True,
                    "result": {"success": False, "has_conflicts": True},
                },
            },
        )

        await engine.evaluate(matching_event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "merge-when-workflow")
        assert instance is not None
        assert instance.variables.get("has_conflicts") is True


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

    @pytest.mark.asyncio
    async def test_set_variable_allowed_in_restricted_step(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """set_variable should be allowed even in steps with restricted allowed_tools.

        Infrastructure tools (set_variable, get_variable) must always pass step
        enforcement so agents can satisfy stop gate conditions.
        """
        _setup_step_workflow(db, manager, instance_mgr, current_step="terminate")
        event = _make_event(
            data={"tool_name": "mcp__gobby__set_variable"},
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_get_variable_allowed_in_restricted_step(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """get_variable should be allowed even in steps with restricted allowed_tools."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="terminate")
        event = _make_event(
            data={"tool_name": "mcp__gobby__get_variable"},
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_toolsearch_allowed_in_restricted_step(
        self, db, manager, engine, instance_mgr
    ) -> None:
        """ToolSearch (Claude Code deferred tool loader) should always be allowed."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            data={"tool_name": "ToolSearch"},
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"
