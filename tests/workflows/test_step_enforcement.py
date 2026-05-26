"""Tests for step-level tool enforcement in the rule engine.

Tests WorkflowStep allowed_tools/blocked_tools/allowed_mcp_tools/blocked_mcp_tools
enforcement and step transitions via on_mcp_success handlers.
"""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowDefinition
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


@pytest.fixture
def db(hub_db: "HubDatabase") -> "HubDatabase":
    return hub_db


@pytest.fixture
def manager(db: "HubDatabase") -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


@pytest.fixture
def engine(db: "HubDatabase") -> RuleEngine:
    return RuleEngine(db)


@pytest.fixture
def instance_mgr(db: "HubDatabase") -> WorkflowInstanceManager:
    return WorkflowInstanceManager(db)


HELPER_BLOCKED_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Grep",
    "Glob",
    "mcp__gobby__set_variable",
]


def _check_agent_tool(tool_name: str, variables: dict[str, Any]) -> HookResponse | None:
    event = _make_event(data={"tool_name": tool_name})
    return RuleEngine(MagicMock())._check_agent_tool_enforcement(event, "test-session", variables)


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
                "gobby-tasks-ops:approve_review",
                "gobby-agents:spawn_agent",
                "gobby-agents:kill_agent",
            ],
            "on_mcp_success": [
                {
                    "server": "gobby-tasks-ops",
                    "tool": "submit_for_review",
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


def _create_session(db: "HubDatabase", session_id: str = "test-session") -> None:
    """Create a minimal session row to satisfy foreign key constraints."""
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        ("project-1", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (session_id, "ext-1", "machine-1", "claude", "project-1"),
    )


def _setup_step_workflow(
    db: "HubDatabase",
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
class TestAgentToolEnforcement:
    """Tests for agent-level tool restrictions."""

    def test_explicit_block_overrides_infra_exempt(self) -> None:
        """Explicit blocked_tools entries should override infrastructure exemptions."""
        variables: dict[str, Any] = {
            "_agent_blocked_tools": ["mcp__gobby__set_variable"],
            "_agent_type": "backend-developer",
        }

        response = _check_agent_tool("mcp__gobby__set_variable", variables)

        assert response is not None
        assert response.decision == "block"
        assert response.reason is not None
        assert "[agent-enforcement:backend-developer]" in response.reason
        assert "Tool 'mcp__gobby__set_variable' is blocked" in response.reason

    def test_infra_exempt_default_when_no_explicit_block(self) -> None:
        """Infrastructure tools should remain allowed unless explicitly blocked."""
        variables: dict[str, Any] = {
            "_agent_blocked_tools": [],
            "_agent_blocked_mcp_tools": ["gobby-memory:create_memory"],
            "_agent_type": "backend-developer",
        }

        response = _check_agent_tool("mcp__gobby__set_variable", variables)

        assert response is None

    def test_blocked_tools_overrides_infra_exempt_for_helper(self) -> None:
        """The memory-recall helper can deny set_variable while keeping get_variable usable."""
        variables: dict[str, Any] = {
            # Mirrors the helper contract from plan section 1.4; that leaf owns YAML drift.
            "_agent_blocked_tools": HELPER_BLOCKED_TOOLS,
            "_agent_type": "memory-recall-helper",
        }

        denied = _check_agent_tool("mcp__gobby__set_variable", variables)
        allowed = _check_agent_tool("mcp__gobby__get_variable", variables)

        assert denied is not None
        assert denied.decision == "block"
        assert denied.reason is not None
        assert "[agent-enforcement:memory-recall-helper]" in denied.reason
        assert "Tool 'mcp__gobby__set_variable' is blocked" in denied.reason
        assert allowed is None


@pytest.mark.unit
class TestStepToolBlocking:
    """Test that step-level tool restrictions are enforced on BEFORE_TOOL events."""

    @pytest.mark.asyncio
    async def test_allowed_tool_passes(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """Tool in allowed_tools list should pass."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(data={"tool_name": "mcp__gobby__call_tool"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_disallowed_tool_blocked(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """Tool NOT in allowed_tools list should be blocked."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(data={"tool_name": "Edit"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "block"
        assert response.reason is not None
        assert "step-enforcement" in response.reason
        assert "claim" in response.reason

    @pytest.mark.parametrize(
        "tool_name",
        [
            "Skill",
            "mcp__codex_apps__github_get_profile",
            "mcp__computer_use__list_apps",
        ],
    )
    @pytest.mark.asyncio
    async def test_skill_load_blocks_native_and_app_tools_with_recovery_guidance(
        self,
        tool_name: str,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """Blocked load-skill mistakes should tell agents the exact proxy path."""
        workflow = {
            "name": "skill-load-feedback",
            "version": "1.0",
            "enabled": False,
            "steps": [
                {
                    "name": "load_skill",
                    "allowed_tools": [
                        "mcp__gobby__call_tool",
                        "mcp__gobby__list_mcp_servers",
                        "mcp__gobby__list_tools",
                        "mcp__gobby__get_tool_schema",
                    ],
                    "allowed_mcp_tools": ["gobby-skills:get_skill"],
                    "on_mcp_success": [
                        {
                            "server": "gobby-skills",
                            "tool": "get_skill",
                            "when": "tool_input.name == 'plan-review'",
                            "action": "set_variable",
                            "variable": "skill_loaded",
                            "value": True,
                        }
                    ],
                }
            ],
        }
        _setup_step_workflow(
            db,
            manager,
            instance_mgr,
            current_step="load_skill",
            workflow_data=workflow,
        )

        response = await engine.evaluate(
            _make_event(data={"tool_name": tool_name}),
            session_id="test-session",
            variables={},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert "gobby-skills:get_skill" in response.reason
        assert 'list_tools("gobby-skills")' in response.reason
        assert 'get_tool_schema("gobby-skills", "get_skill")' in response.reason
        assert 'call_tool("gobby-skills", "get_skill", {"name": "plan-review"})' in response.reason
        assert tool_name in response.reason

    @pytest.mark.asyncio
    async def test_all_tools_allowed_when_set(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """When allowed_tools is 'all', any native tool should pass."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="implement")
        event = _make_event(data={"tool_name": "Edit"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_blocked_tools_enforced(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
        assert response.reason is not None
        assert "blocked" in response.reason.lower()

    @pytest.mark.asyncio
    async def test_discovery_tools_always_pass(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
    async def test_no_step_workflow_allows_all(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
    ) -> None:
        """Without an active step workflow, all tools should pass."""
        event = _make_event(data={"tool_name": "Edit"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"


@pytest.mark.unit
class TestStepMCPToolBlocking:
    """Test MCP tool restrictions (allowed_mcp_tools/blocked_mcp_tools)."""

    @pytest.mark.asyncio
    async def test_allowed_mcp_tool_passes(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
    async def test_disallowed_mcp_tool_blocked(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
        assert response.reason is not None
        assert "gobby-tasks:close_task" in response.reason

    @pytest.mark.asyncio
    async def test_skill_load_blocks_wrong_mcp_tool_with_recovery_guidance(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """A wrong proxy call during load_skill should name the get_skill recovery call."""
        workflow = {
            "name": "skill-load-mcp-feedback",
            "version": "1.0",
            "enabled": False,
            "steps": [
                {
                    "name": "load_skill",
                    "allowed_tools": ["mcp__gobby__call_tool"],
                    "allowed_mcp_tools": ["gobby-skills:get_skill"],
                    "on_mcp_success": [
                        {
                            "server": "gobby-skills",
                            "tool": "get_skill",
                            "when": "tool_input.name == 'plan-review'",
                            "action": "set_variable",
                            "variable": "skill_loaded",
                            "value": True,
                        }
                    ],
                }
            ],
        }
        _setup_step_workflow(
            db,
            manager,
            instance_mgr,
            current_step="load_skill",
            workflow_data=workflow,
        )
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "github",
                    "tool_name": "get_profile",
                },
            }
        )

        response = await engine.evaluate(event, session_id="test-session", variables={})

        assert response.decision == "block"
        assert response.reason is not None
        assert "github:get_profile" in response.reason
        assert "gobby-skills:get_skill" in response.reason
        assert 'call_tool("gobby-skills", "get_skill", {"name": "plan-review"})' in response.reason

    @pytest.mark.asyncio
    async def test_blocked_mcp_tool_enforced(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
    async def test_mcp_discovery_tools_always_pass(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
    async def test_wildcard_mcp_tool_pattern(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
    async def test_on_mcp_success_sets_variable(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
    async def test_no_transition_on_failure(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """submit_for_review in implement step should transition to terminate."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="implement")
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks-ops",
                    "tool_name": "submit_for_review",
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
    async def test_implement_to_terminate_transition_for_codex_call_tool_alias(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """Codex's alternate call_tool alias should fire on_mcp_success handlers."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="implement")
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "mcp_gobby_call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks-ops",
                    "tool_name": "submit_for_review",
                },
            },
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        assert instance.current_step == "terminate"
        assert instance.variables.get("review_submitted") is True
        assert response.context is not None
        assert "implement" in response.context
        assert "terminate" in response.context

    @pytest.mark.asyncio
    async def test_no_transition_for_unmatched_tool(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
                            "server": "gobby-tasks-ops",
                            "tool": "submit_for_review",
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
                    "server_name": "gobby-tasks-ops",
                    "tool_name": "submit_for_review",
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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

    @pytest.mark.asyncio
    async def test_session_var_does_not_shadow_instance_var_for_transition(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """Session-scoped variables must NOT drive workflow transitions.

        Regression for session #3277: the parent planner handed a claimed
        task to the spawned plan-adversary, which made
        _session_start.py write ``task_claimed=True`` into the child's
        session variables. Before the fix, the merge order in
        ``_process_step_after_tool`` let that session value shadow the
        instance's own ``task_claimed=False``, firing the
        ``claim -> implement`` transition on the adversary's first
        successful MCP tool — without the workflow's own
        ``on_mcp_success`` handler for ``claim_task`` ever running.
        """
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        # Confirm the instance starts with task_claimed=False.
        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        assert instance.variables.get("task_claimed") is False

        # Simulate the session-start handoff: a session-scoped variable
        # with the same name as a workflow step_variable, pre-set to True
        # before the workflow ever runs its own handler.
        variables: dict[str, Any] = {"task_claimed": True}

        # Call a tool that does NOT match the workflow's claim_task
        # handler — so no handler can legitimately flip task_claimed.
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

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        # Must stay in claim: instance.variables wins over session vars.
        assert instance.current_step == "claim"
        assert instance.variables.get("task_claimed") is False

    @pytest.mark.asyncio
    async def test_handler_set_instance_var_transitions_despite_session_false(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """Workflow-local handler state must transition even when session var is False.

        Inverse direction of the precedence fix: after the workflow's own
        ``on_mcp_success`` handler sets ``instance.variables['task_claimed']``
        to True, the transition must fire even if a session-scoped variable
        with the same name is False. Confirms the spread order doesn't
        flip the other way and break the happy path.
        """
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        variables: dict[str, Any] = {"task_claimed": False}

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

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "developer-workflow")
        assert instance is not None
        assert instance.current_step == "implement"
        assert instance.variables.get("task_claimed") is True

    @pytest.mark.asyncio
    async def test_session_only_var_remains_readable_in_transition_when(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """Session-only variables (not present in instance.variables) must stay readable.

        The precedence flip must not hide session-level variables that the
        instance doesn't claim. Workflows can still transition on genuinely
        session-scoped signals (e.g. ``vars.stop_attempts``) as long as
        there's no name collision with their own step_variables.
        """
        workflow = {
            "name": "session-var-gated",
            "version": "1.0",
            "enabled": False,
            "variables": {"handler_var": False},  # no `kick` — session-only
            "steps": [
                {
                    "name": "waiting",
                    "allowed_tools": "all",
                    "on_mcp_success": [
                        {
                            "server": "gobby-tasks",
                            "tool": "get_task",
                            "action": "set_variable",
                            "variable": "handler_var",
                            "value": True,
                        }
                    ],
                    "transitions": [{"to": "done", "when": "vars.kick"}],
                },
                {"name": "done", "allowed_tools": "all"},
            ],
        }
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="waiting", workflow_data=workflow
        )
        # Session-only variable (not in instance.variables) that the
        # transition's `when` reads.
        variables: dict[str, Any] = {"kick": True}

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

        await engine.evaluate(event, session_id="test-session", variables=variables)

        instance = instance_mgr.get_instance("test-session", "session-var-gated")
        assert instance is not None
        assert instance.current_step == "done"

    @pytest.mark.asyncio
    async def test_send_keys_bypasses_step_allow_list(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """Operator tool send_keys must bypass step MCP allow-lists.

        Regression for the dogfood block on session #3277: the developer
        tried to interrogate a stuck plan-adversary via
        ``gobby-sessions:send_keys`` from the web app, but the adversary's
        ``terminate`` step only whitelists ``gobby-agents:kill_agent``.
        Operator/debug channels must be exempt so humans can always reach
        a running session regardless of its workflow step.
        """
        _setup_step_workflow(db, manager, instance_mgr, current_step="terminate")
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-sessions",
                    "tool_name": "send_keys",
                    "arguments": {"keys": "ls\n"},
                },
            },
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)

        assert response.decision == "allow", (
            f"send_keys must bypass terminate's narrow allow-list; "
            f"got decision={response.decision!r} reason={response.reason!r}"
        )

    @pytest.mark.asyncio
    async def test_capture_output_bypasses_step_allow_list(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """Operator tool capture_output must bypass step MCP allow-lists."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="terminate")
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-sessions",
                    "tool_name": "capture_output",
                    "arguments": {},
                },
            },
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)

        assert response.decision == "allow", (
            f"capture_output must bypass terminate's narrow allow-list; "
            f"got decision={response.decision!r} reason={response.reason!r}"
        )


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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
    async def test_on_mcp_error_with_nested_result(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
    async def test_string_tool_output_parsed(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="merge", workflow_data=workflow
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
    async def test_kill_agent_allowed_in_terminate(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
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
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        """ToolSearch (Claude Code deferred tool loader) should always be allowed."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            data={"tool_name": "ToolSearch"},
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id="test-session", variables=variables)
        assert response.decision == "allow"


@pytest.mark.unit
class TestStepBeforeMcpHandlers:
    """Test step handlers that run before allowed MCP tools execute."""

    @pytest.mark.asyncio
    async def test_on_mcp_before_enforces_retry_counter_per_conflict(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        workflow = {
            "name": "merge-retry-test",
            "version": "1.0",
            "variables": {"merge_resolve_attempts": []},
            "steps": [
                {
                    "name": "merge",
                    "allowed_tools": ["mcp__gobby__call_tool"],
                    "allowed_mcp_tools": ["gobby-merge:merge_resolve"],
                    "on_mcp_before": [
                        {
                            "server": "gobby-merge",
                            "tool": "merge_resolve",
                            "action": "block",
                            "when": (
                                "vars.get('merge_resolve_attempts', [])"
                                ".count(tool_input.get('conflict_id', '')) >= 3"
                            ),
                            "reason": "retry cap reached",
                        },
                        {
                            "server": "gobby-merge",
                            "tool": "merge_resolve",
                            "action": "set_variable",
                            "variable": "merge_resolve_attempts",
                            "value": (
                                "vars.get('merge_resolve_attempts', []) "
                                "+ [tool_input.get('conflict_id', '')]"
                            ),
                        },
                    ],
                }
            ],
        }
        _setup_step_workflow(
            db,
            manager,
            instance_mgr,
            current_step="merge",
            workflow_data=workflow,
        )
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-merge",
                    "tool_name": "merge_resolve",
                    "arguments": {"conflict_id": "mc-one"},
                },
            }
        )
        variables: dict[str, Any] = {}

        for expected_count in (1, 2, 3):
            response = await engine.evaluate(event, session_id="test-session", variables=variables)
            assert response.decision == "allow"
            instance = instance_mgr.get_instance("test-session", "merge-retry-test")
            assert instance is not None
            assert instance.variables["merge_resolve_attempts"].count("mc-one") == expected_count

        other_conflict_event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-merge",
                    "tool_name": "merge_resolve",
                    "arguments": {"conflict_id": "mc-two"},
                },
            }
        )
        response = await engine.evaluate(
            other_conflict_event, session_id="test-session", variables=variables
        )

        assert response.decision == "allow"

        response = await engine.evaluate(event, session_id="test-session", variables=variables)

        assert response.decision == "block"
        assert response.reason is not None
        assert "retry cap reached" in response.reason
        instance = instance_mgr.get_instance("test-session", "merge-retry-test")
        assert instance is not None
        assert instance.variables["merge_resolve_attempts"].count("mc-one") == 3
        assert instance.variables["merge_resolve_attempts"].count("mc-two") == 1

    @pytest.mark.asyncio
    async def test_merge_retry_counter_ignores_retry_later_tool_results(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        workflow = {
            "name": "merge-retry-test",
            "version": "1.0",
            "variables": {"merge_resolve_attempts": []},
            "steps": [
                {
                    "name": "merge",
                    "allowed_tools": ["mcp__gobby__call_tool"],
                    "allowed_mcp_tools": ["gobby-merge:merge_resolve"],
                    "on_mcp_before": [
                        {
                            "server": "gobby-merge",
                            "tool": "merge_resolve",
                            "action": "block",
                            "when": (
                                "vars.get('merge_resolve_attempts', [])"
                                ".count(tool_input.get('conflict_id', '')) >= 3"
                            ),
                            "reason": "retry cap reached",
                        },
                    ],
                    "on_mcp_success": [
                        {
                            "server": "gobby-merge",
                            "tool": "merge_resolve",
                            "action": "set_variable",
                            "variable": "merge_resolve_attempts",
                            "value": (
                                "vars.get('merge_resolve_attempts', []) "
                                "+ [tool_input.get('conflict_id', '')]"
                            ),
                        },
                    ],
                    "on_mcp_error": [
                        {
                            "server": "gobby-merge",
                            "tool": "merge_resolve",
                            "when": "not bool(tool_output.get('retry_later'))",
                            "action": "set_variable",
                            "variable": "merge_resolve_attempts",
                            "value": (
                                "vars.get('merge_resolve_attempts', []) "
                                "+ [tool_input.get('conflict_id', '')]"
                            ),
                        },
                    ],
                }
            ],
        }
        _setup_step_workflow(
            db,
            manager,
            instance_mgr,
            current_step="merge",
            workflow_data=workflow,
        )
        tool_input = {
            "server_name": "gobby-merge",
            "tool_name": "merge_resolve",
            "arguments": {"conflict_id": "mc-one"},
        }
        before_event = _make_event(
            data={"tool_name": "mcp__gobby__call_tool", "tool_input": tool_input}
        )

        def after_event(tool_output: dict[str, Any]) -> HookEvent:
            return _make_event(
                event_type=HookEventType.AFTER_TOOL,
                data={
                    "tool_name": "mcp__gobby__call_tool",
                    "tool_input": tool_input,
                    "tool_output": tool_output,
                },
            )

        variables: dict[str, Any] = {}

        response = await engine.evaluate(
            before_event, session_id="test-session", variables=variables
        )
        assert response.decision == "allow"

        await engine.evaluate(
            after_event({"success": False, "retry_later": True}),
            session_id="test-session",
            variables=variables,
        )
        instance = instance_mgr.get_instance("test-session", "merge-retry-test")
        assert instance is not None
        assert instance.variables["merge_resolve_attempts"].count("mc-one") == 0

        for expected_count, output in (
            (1, {"success": False, "error": "AI resolution failed"}),
            (2, {"success": True}),
            (3, {"success": False, "error": "ReadTimeout: (no message)"}),
        ):
            response = await engine.evaluate(
                before_event,
                session_id="test-session",
                variables=variables,
            )
            assert response.decision == "allow"
            await engine.evaluate(
                after_event(output),
                session_id="test-session",
                variables=variables,
            )
            instance = instance_mgr.get_instance("test-session", "merge-retry-test")
            assert instance is not None
            assert instance.variables["merge_resolve_attempts"].count("mc-one") == expected_count

        response = await engine.evaluate(
            before_event, session_id="test-session", variables=variables
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert "retry cap reached" in response.reason
        instance = instance_mgr.get_instance("test-session", "merge-retry-test")
        assert instance is not None
        assert instance.variables["merge_resolve_attempts"].count("mc-one") == 3

    @pytest.mark.asyncio
    async def test_duplicate_proxy_before_tool_does_not_consume_retry_budget(
        self,
        db: "HubDatabase",
        manager: LocalWorkflowDefinitionManager,
        engine: RuleEngine,
        instance_mgr: WorkflowInstanceManager,
    ) -> None:
        workflow = {
            "name": "merge-retry-test",
            "version": "1.0",
            "variables": {"merge_resolve_attempts": []},
            "steps": [
                {
                    "name": "merge",
                    "allowed_tools": ["mcp__gobby__call_tool"],
                    "allowed_mcp_tools": ["gobby-merge:merge_resolve"],
                    "on_mcp_before": [
                        {
                            "server": "gobby-merge",
                            "tool": "merge_resolve",
                            "action": "block",
                            "when": (
                                "vars.get('merge_resolve_attempts', [])"
                                ".count(tool_input.get('conflict_id', '')) >= 3"
                            ),
                            "reason": "retry cap reached",
                        },
                        {
                            "server": "gobby-merge",
                            "tool": "merge_resolve",
                            "action": "set_variable",
                            "variable": "merge_resolve_attempts",
                            "value": (
                                "vars.get('merge_resolve_attempts', []) "
                                "+ [tool_input.get('conflict_id', '')]"
                            ),
                        },
                    ],
                }
            ],
        }
        _setup_step_workflow(
            db,
            manager,
            instance_mgr,
            current_step="merge",
            workflow_data=workflow,
        )
        event_data = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-merge",
                "tool_name": "merge_resolve",
                "arguments": {"conflict_id": "mc-one"},
            },
        }
        variables: dict[str, Any] = {}

        for expected_count in (1, 2, 3):
            response = await engine.evaluate(
                _make_event(data=event_data),
                session_id="test-session",
                variables=variables,
            )
            assert response.decision == "allow"

            duplicate_response = await engine.evaluate(
                _make_event(
                    data=event_data,
                    metadata={"_mcp_proxy_duplicate_before_tool": True},
                ),
                session_id="test-session",
                variables=variables,
            )
            assert duplicate_response.decision == "allow"

            instance = instance_mgr.get_instance("test-session", "merge-retry-test")
            assert instance is not None
            assert instance.variables["merge_resolve_attempts"].count("mc-one") == expected_count

        response = await engine.evaluate(
            _make_event(data=event_data),
            session_id="test-session",
            variables=variables,
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert "retry cap reached" in response.reason
