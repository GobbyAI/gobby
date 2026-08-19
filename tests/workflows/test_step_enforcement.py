"""Tests for step-level tool enforcement in the rule engine.

Tests WorkflowStep allowed_tools/blocked_tools/allowed_mcp_tools/blocked_mcp_tools
enforcement and step transitions via on_mcp_success handlers.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.enforcement.blocking import canonical_gobby_tool_name, is_gobby_call_tool
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

# Session/project/instance id columns are native uuid in PostgreSQL; synthetic
# ids like SESSION_ID would fail with `invalid input syntax for type uuid`.
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


AGENT_BLOCKED_TOOLS = [
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
    return RuleEngine(MagicMock())._check_agent_tool_enforcement(event, SESSION_ID, variables)


def _make_event(
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
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


def _create_session(db: "HubDatabase", session_id: str = SESSION_ID) -> None:
    """Create a minimal session row to satisfy foreign key constraints."""
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (PROJECT_ID, "test-project"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (session_id, "ext-1", "21000000-0000-4000-8000-000000000001", "claude", PROJECT_ID),
    )


def _setup_step_workflow(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
    session_id: str = SESSION_ID,
    current_step: str = "claim",
    workflow_data: dict[str, Any] | None = None,
) -> None:
    """Insert a workflow definition and create an active instance on a session."""
    _create_session(db, session_id)

    data = workflow_data or _DEVELOPER_WORKFLOW
    raw_variables = data.get("variables")
    variables = dict(raw_variables) if isinstance(raw_variables, dict) else {}
    raw_steps = data.get("steps")
    steps = list(raw_steps) if isinstance(raw_steps, list) else []
    step_workflow = {
        "variables": variables,
        "exit_condition": data.get("exit_condition"),
        "steps": steps,
    }
    parent = {
        key: value
        for key, value in data.items()
        if key not in {"steps", "variables", "exit_condition", "step_workflow"}
    }
    row = manager.upsert_with_steps(str(data["name"]), parent, step_workflow)
    body = AgentDefinitionBody.model_validate(
        {**parent, "name": data["name"], "step_workflow": step_workflow}
    )
    instance = build_step_instance(
        body,
        session_id=session_id,
        step_workflow_id=row.step_workflow_id,
        current_step=current_step,
        variables=variables,
    )
    instance.step_entered_at = datetime.now(UTC)
    instance_mgr.save(instance)


def _running_rule_engine(
    db: "HubDatabase",
) -> tuple[RuleEngine, LocalAgentRunManager, str]:
    run_manager = LocalAgentRunManager(db)
    run = run_manager.create(
        parent_session_id=SESSION_ID,
        child_session_id=SESSION_ID,
        provider="claude",
        prompt="test",
    )
    assert run_manager.start(run.id) is not None
    runner = MagicMock()
    runner.run_storage = run_manager
    return RuleEngine(db, runner=runner), run_manager, run.id


@pytest.mark.asyncio
async def test_third_denial_terminates_run(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    """A repeated step denial terminalizes its run without advancing the guarded step."""
    _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
    rule_engine, run_manager, run_id = _running_rule_engine(db)
    event = _make_event(data={"tool_name": "Edit"})

    for _ in range(2):
        response = await rule_engine.evaluate(event, session_id=SESSION_ID, variables={})
        assert response.decision == "block"
        active_run = run_manager.get(run_id)
        assert active_run is not None
        assert active_run.status == "running"

    response = await rule_engine.evaluate(event, session_id=SESSION_ID, variables={})

    assert response.decision == "block"
    assert response.reason is not None
    assert "terminal blocked state" in response.reason
    blocked_run = run_manager.get(run_id)
    assert blocked_run is not None
    assert blocked_run.status == "error"
    assert blocked_run.error is not None
    assert "blocked after 3 identical enforcement denials" in blocked_run.error
    instance = instance_mgr.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.current_step == "claim"


@pytest.mark.asyncio
async def test_unrelated_allowed_calls_do_not_reset_denial_counter(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    """Allowed polling between identical denials cannot prevent terminalization."""
    _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
    rule_engine, run_manager, run_id = _running_rule_engine(db)
    denied_event = _make_event(data={"tool_name": "Edit"})
    allowed_event = _make_event(data={"tool_name": "mcp__gobby__list_tools"})

    for denial_number in range(1, 4):
        denied = await rule_engine.evaluate(
            denied_event,
            session_id=SESSION_ID,
            variables={},
        )
        assert denied.decision == "block"
        if denial_number < 3:
            allowed = await rule_engine.evaluate(
                allowed_event,
                session_id=SESSION_ID,
                variables={},
            )
            assert allowed.decision == "allow"

    blocked_run = run_manager.get(run_id)
    assert blocked_run is not None
    assert blocked_run.status == "error"


@pytest.mark.asyncio
async def test_allowed_target_resets_only_its_denial_counter(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    """A target that becomes allowed clears its own counter and preserves other targets."""
    _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
    rule_engine, _run_manager, _run_id = _running_rule_engine(db)

    for tool_name in ("Edit", "Write"):
        denied = await rule_engine.evaluate(
            _make_event(data={"tool_name": tool_name}),
            session_id=SESSION_ID,
            variables={},
        )
        assert denied.decision == "block"

    instance = instance_mgr.get_for_session(SESSION_ID)
    assert instance is not None
    snapshot = instance.snapshot.model_dump()
    snapshot["steps"][0]["allowed_tools"] = list(snapshot["steps"][0]["allowed_tools"]) + ["Edit"]
    instance.snapshot = type(instance.snapshot).model_validate(snapshot)
    instance_mgr.replace_for_session(instance)

    allowed = await rule_engine.evaluate(
        _make_event(data={"tool_name": "Edit"}),
        session_id=SESSION_ID,
        variables={},
    )

    assert allowed.decision == "allow"
    instance = instance_mgr.get_for_session(SESSION_ID)
    assert instance is not None
    state = instance.variables["_enforcement_denial_counts"]
    assert isinstance(state, dict)
    counts = state["counts"]
    assert isinstance(counts, dict)
    assert len(counts) == 1
    remaining_key = json.loads(next(iter(counts)))
    assert remaining_key["target"] == "tool:write"


@pytest.mark.asyncio
async def test_denial_counter_key_and_resets(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    """Denials use the full key and a new step revision starts a fresh ledger."""
    _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
    rule_engine, run_manager, run_id = _running_rule_engine(db)

    for tool_name in ("Edit", "Write"):
        response = await rule_engine.evaluate(
            _make_event(data={"tool_name": tool_name}),
            session_id=SESSION_ID,
            variables={},
        )
        assert response.decision == "block"

    instance = instance_mgr.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.step_entered_at is not None
    state = instance.variables["_enforcement_denial_counts"]
    assert isinstance(state, dict)
    counts = state["counts"]
    assert isinstance(counts, dict)
    assert len(counts) == 2
    decoded_keys = [json.loads(key) for key in counts]
    assert {key["agent_run_id"] for key in decoded_keys} == {run_id}
    assert {key["workflow_instance_id"] for key in decoded_keys} == {instance.id}
    assert {key["step_revision"] for key in decoded_keys} == {instance.step_entered_at.isoformat()}
    assert {key["rule"] for key in decoded_keys} == {"step-native-tool-allowlist"}
    assert {key["target"] for key in decoded_keys} == {"tool:edit", "tool:write"}

    await rule_engine.evaluate(
        _make_event(data={"tool_name": "Edit"}),
        session_id=SESSION_ID,
        variables={},
    )
    instance = instance_mgr.get_for_session(SESSION_ID)
    assert instance is not None
    prior_revision = instance.step_entered_at
    instance.current_step = "implement"
    instance.step_entered_at = datetime.now(UTC)
    instance_mgr.save(instance)

    transitioned_denial = await rule_engine.evaluate(
        _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#1"},
                },
            }
        ),
        session_id=SESSION_ID,
        variables={},
    )
    assert transitioned_denial.decision == "block"
    instance = instance_mgr.get_for_session(SESSION_ID)
    assert instance is not None
    assert instance.step_entered_at != prior_revision
    assert instance.step_entered_at is not None
    state = instance.variables["_enforcement_denial_counts"]
    counts = state["counts"]
    assert len(counts) == 1
    transitioned_key = json.loads(next(iter(counts)))
    assert transitioned_key["rule"] == "step-mcp-tool-block"
    assert transitioned_key["target"] == "mcp:gobby-tasks:close_task"
    assert transitioned_key["step_revision"] == instance.step_entered_at.isoformat()
    active_run = run_manager.get(run_id)
    assert active_run is not None
    assert active_run.status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_data", "variables"),
    [
        ({"tool_name": "Edit"}, {"_agent_blocked_tools": ["Edit"]}),
        (
            {
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-agents",
                    "tool_name": "spawn_agent",
                    "arguments": {},
                },
            },
            {"_agent_blocked_mcp_tools": ["gobby-agents:spawn_agent"]},
        ),
    ],
)
async def test_agent_denials_carry_guidance_and_count(
    event_data: dict[str, Any],
    variables: dict[str, Any],
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    """Agent-level native and MCP blocks use the same guided terminal counter."""
    _setup_step_workflow(db, manager, instance_mgr, current_step="implement")
    rule_engine, run_manager, run_id = _running_rule_engine(db)
    variables["_agent_type"] = "backend-developer"

    responses = [
        await rule_engine.evaluate(
            _make_event(data=event_data),
            session_id=SESSION_ID,
            variables=variables,
        )
        for _ in range(3)
    ]

    assert all(response.decision == "block" for response in responses)
    final_reason = responses[-1].reason
    assert final_reason is not None
    assert "continue with an operation permitted by the agent definition" in final_reason
    assert "terminal blocked state" in final_reason
    blocked_run = run_manager.get(run_id)
    assert blocked_run is not None
    assert blocked_run.status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_data",
    [
        {
            "tool_name": "mcp__gobby__set_variable",
            "tool_input": {"name": "tool_block_pending", "value": True},
        },
        {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby",
                "tool_name": "set_variable",
                "arguments": {"name": "tool_block_pending", "value": True},
            },
        },
    ],
)
async def test_reserved_variable_denial_is_variable_specific(
    event_data: dict[str, Any],
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    engine: RuleEngine,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    """Native and MCP set_variable routes tell the agent to abandon only one variable."""
    _setup_step_workflow(db, manager, instance_mgr, current_step="implement")

    response = await engine.evaluate(
        _make_event(data=event_data),
        session_id=SESSION_ID,
        variables={},
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "Abandon only this write to variable 'tool_block_pending'" in response.reason
    assert "set_variable remains available for other permitted variables" in response.reason
    assert "This capability is unavailable for the rest" not in response.reason


def test_zsh_quoting_guidance_contract() -> None:
    """The Bash reference and adversary instructions require safe zsh CSS quoting."""
    repo_root = Path(__file__).parents[2]
    bash_guidance = (
        repo_root / "src/gobby/install/shared/skills/bash/references/quoting-and-data.md"
    ).read_text()
    adversary_guidance = (
        repo_root / "src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml"
    ).read_text()

    for required in ("zsh", "single-quote", "@theme", "@custom-variant", "parenthesized", "#"):
        assert required in bash_guidance.casefold()
        assert required in adversary_guidance.casefold()


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

    def test_blocked_tools_overrides_infra_exempt_for_agent(self) -> None:
        """Agent-level blocked_tools can deny set_variable while get_variable stays usable."""
        variables: dict[str, Any] = {
            "_agent_blocked_tools": AGENT_BLOCKED_TOOLS,
            "_agent_type": "locked-down-agent",
        }

        denied = _check_agent_tool("mcp__gobby__set_variable", variables)
        allowed = _check_agent_tool("mcp__gobby__get_variable", variables)

        assert denied is not None
        assert denied.decision == "block"
        assert denied.reason is not None
        assert "[agent-enforcement:locked-down-agent]" in denied.reason
        assert "Tool 'mcp__gobby__set_variable' is blocked" in denied.reason
        assert allowed is None


@pytest.mark.unit
class TestStepToolBlocking:
    """Test that step-level tool restrictions are enforced on BEFORE_TOOL events."""

    @pytest.mark.asyncio
    async def test_allowed_tool_passes(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """Tool in allowed_tools list should pass."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(data={"tool_name": "mcp__gobby__call_tool"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_disallowed_tool_blocked(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """Tool NOT in allowed_tools list should be blocked."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(data={"tool_name": "Edit"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
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
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """Blocked load-skill mistakes should give the direct bootstrap call."""
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
            session_id=SESSION_ID,
            variables={},
        )

        assert response.decision == "block"
        assert response.reason is not None
        delimiter = "\nDuring this skill-loading step:"
        assert delimiter in response.reason
        guidance = response.reason.split(delimiter, maxsplit=1)[1]
        assert "list_tools" not in guidance
        assert "get_tool_schema" not in guidance
        assert "fully read the skill" in guidance
        assert "own outer tool result" in guidance
        assert 'call_tool("gobby-skills", "get_skill", {"name":"plan-review"})' in guidance
        assert tool_name in response.reason

    @pytest.mark.asyncio
    async def test_all_tools_allowed_when_set(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """When allowed_tools is 'all', any native tool should pass."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="implement")
        event = _make_event(data={"tool_name": "Edit"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_blocked_tools_enforced(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "block"
        assert response.reason is not None
        assert "blocked" in response.reason.lower()

    @pytest.mark.asyncio
    async def test_discovery_tools_always_pass(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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
            response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
            assert response.decision == "allow", f"Discovery tool {tool} should pass"

    @pytest.mark.asyncio
    async def test_no_step_workflow_allows_all(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
    ) -> None:
        """Without an active step workflow, all tools should pass."""
        event = _make_event(data={"tool_name": "Edit"})
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"


@pytest.mark.unit
class TestStepMCPToolBlocking:
    """Test MCP tool restrictions (allowed_mcp_tools/blocked_mcp_tools)."""

    @pytest.mark.asyncio
    async def test_allowed_mcp_tool_passes(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_disallowed_mcp_tool_blocked(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "block"
        assert response.reason is not None
        assert "gobby-tasks:close_task" in response.reason
        assert "Abandon this call" in response.reason
        assert "do not retry the same blocked tool in this step" in response.reason

    @pytest.mark.parametrize(
        ("allowed_mcp_tools", "blocked_mcp_tools", "expected_decision"),
        [
            (["gobby-tasks:claim_task"], [], "allow"),
            (
                ["gobby-tasks:claim_task"],
                ["gobby-sessions:compact_self"],
                "block",
            ),
            ("all", [], "allow"),
        ],
        ids=["restrictive-allowlist", "explicit-block", "all-tools"],
    )
    @pytest.mark.asyncio
    async def test_compact_self_step_enforcement(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
        allowed_mcp_tools: list[str] | str,
        blocked_mcp_tools: list[str],
        expected_decision: str,
    ) -> None:
        step: dict[str, Any] = {
            "name": "work",
            "allowed_mcp_tools": allowed_mcp_tools,
            "blocked_mcp_tools": blocked_mcp_tools,
        }
        workflow = {
            "name": "test-compact-self",
            "version": "2.0",
            "enabled": False,
            "steps": [step],
        }
        _setup_step_workflow(
            db,
            manager,
            instance_mgr,
            current_step="work",
            workflow_data=workflow,
        )
        event = _make_event(
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-sessions",
                    "tool_name": "compact_self",
                },
            }
        )

        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == expected_decision
        if blocked_mcp_tools:
            assert response.reason is not None
            assert "blocked" in response.reason.lower()

    @pytest.mark.asyncio
    async def test_skill_load_blocks_wrong_mcp_tool_with_recovery_guidance(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "block"
        assert response.reason is not None
        assert "github:get_profile" in response.reason
        assert "gobby-skills:get_skill" in response.reason
        assert 'call_tool("gobby-skills", "get_skill", {"name":"plan-review"})' in response.reason

    @pytest.mark.asyncio
    async def test_blocked_mcp_tool_enforced(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_mcp_discovery_tools_always_pass(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_wildcard_mcp_tool_pattern(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"


@pytest.mark.unit
class TestStepTransitions:
    """Test step transitions via on_mcp_success handlers."""

    @pytest.mark.asyncio
    async def test_on_mcp_success_sets_variable(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        # Check the instance was updated
        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.variables.get("task_claimed") is True

    @pytest.mark.asyncio
    async def test_transition_fires_after_variable_set(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
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
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.current_step == "claim"  # No transition

    @pytest.mark.asyncio
    async def test_implement_to_terminate_transition(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
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
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
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
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.current_step == "implement"  # No change

    @pytest.mark.asyncio
    async def test_no_transition_returns_no_context(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        # No transition means no transition context
        assert response.context is None or "Step transition" not in response.context

    @pytest.mark.asyncio
    async def test_transition_includes_status_message(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.context is not None
        assert "working" in response.context
        assert "finished" in response.context
        assert "Call kill_agent to terminate." in response.context

    @pytest.mark.asyncio
    async def test_on_mcp_success_handler_when_gates_variable_update(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
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

        await engine.evaluate(matching_event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.variables.get("skill_loaded") is True

    @pytest.mark.asyncio
    async def test_session_var_does_not_shadow_instance_var_for_transition(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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
        instance = instance_mgr.get_for_session(SESSION_ID)
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        # Must stay in claim: instance.variables wins over session vars.
        assert instance.current_step == "claim"
        assert instance.variables.get("task_claimed") is False

    @pytest.mark.asyncio
    async def test_handler_set_instance_var_transitions_despite_session_false(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.current_step == "implement"
        assert instance.variables.get("task_claimed") is True

    @pytest.mark.asyncio
    async def test_session_only_var_remains_readable_in_transition_when(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.current_step == "done"

    @pytest.mark.asyncio
    async def test_exit_condition_uses_merged_variables_for_both_aliases(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        workflow = {
            "name": "merged-exit-vars",
            "version": "1.0",
            "enabled": False,
            "variables": {"handler_var": False, "collision": False},
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
                    "transitions": [{"to": "done", "when": "vars.handler_var"}],
                },
                {"name": "done", "allowed_tools": "all"},
            ],
            "exit_condition": (
                "current_step == 'done' and vars.exit_ready and variables.exit_ready "
                "and vars.collision == variables.collision"
            ),
        }
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="waiting", workflow_data=workflow
        )
        variables: dict[str, Any] = {"exit_ready": True, "collision": True}
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert variables["step_workflow_complete"] is True

    @pytest.mark.asyncio
    async def test_send_keys_bypasses_step_allow_list(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.decision == "allow", (
            f"send_keys must bypass terminate's narrow allow-list; "
            f"got decision={response.decision!r} reason={response.reason!r}"
        )

    @pytest.mark.asyncio
    async def test_capture_output_bypasses_step_allow_list(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

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
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        # merge_complete should NOT be set (on_mcp_success was skipped)
        assert instance.variables.get("merge_complete") is False

    @pytest.mark.asyncio
    async def test_on_mcp_error_fires_on_tool_failure(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.variables.get("has_conflicts") is True
        # Transition to resolve_conflicts should fire
        assert instance.current_step == "resolve_conflicts"

    @pytest.mark.asyncio
    async def test_on_mcp_success_fires_on_tool_success(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.variables.get("merge_complete") is True
        assert instance.variables.get("has_conflicts") is False
        assert instance.current_step == "done"

    @pytest.mark.asyncio
    async def test_on_mcp_error_with_nested_result(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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
                "tool_outcome": {"status": "failed", "provenance": "test.fixture"},
                "_tool_outcome_trust": "provider_contract",
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.variables.get("has_conflicts") is True
        assert instance.current_step == "resolve_conflicts"

    @pytest.mark.asyncio
    async def test_no_tool_output_uses_on_mcp_success(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        # Without tool_output, should fall through to on_mcp_success
        assert instance.variables.get("merge_complete") is True
        assert instance.current_step == "done"

    @pytest.mark.asyncio
    async def test_string_tool_output_parsed(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.variables.get("has_conflicts") is True
        assert instance.current_step == "resolve_conflicts"

    @pytest.mark.asyncio
    async def test_on_mcp_error_handler_when_gates_variable_update(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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
                "tool_outcome": {"status": "failed", "provenance": "test.fixture"},
                "_tool_outcome_trust": "provider_contract",
            },
        )
        variables: dict[str, Any] = {}

        await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
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
                "tool_outcome": {"status": "failed", "provenance": "test.fixture"},
                "_tool_outcome_trust": "provider_contract",
            },
        )

        await engine.evaluate(matching_event, session_id=SESSION_ID, variables=variables)

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.variables.get("has_conflicts") is True


@pytest.mark.unit
class TestStepEnforcementAfterTransition:
    """Test that tool restrictions update after a step transition."""

    @pytest.mark.asyncio
    async def test_tools_restricted_after_transition_to_terminate(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_kill_agent_allowed_in_terminate(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_set_variable_allowed_in_restricted_step(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_get_variable_allowed_in_restricted_step(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """get_variable should be allowed even in steps with restricted allowed_tools."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="terminate")
        event = _make_event(
            data={"tool_name": "mcp__gobby__get_variable"},
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_toolsearch_allowed_in_restricted_step(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """ToolSearch (Claude Code deferred tool loader) should always be allowed."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            data={"tool_name": "ToolSearch"},
        )
        variables: dict[str, Any] = {}

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"


@pytest.mark.unit
class TestStepBeforeMcpHandlers:
    """Test step handlers that run before allowed MCP tools execute."""

    @pytest.mark.asyncio
    async def test_on_mcp_before_enforces_retry_counter_per_conflict(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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
            response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
            assert response.decision == "allow"
            instance = instance_mgr.get_for_session(SESSION_ID)
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
            other_conflict_event, session_id=SESSION_ID, variables=variables
        )

        assert response.decision == "allow"

        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.decision == "block"
        assert response.reason is not None
        assert "retry cap reached" in response.reason
        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.variables["merge_resolve_attempts"].count("mc-one") == 3
        assert instance.variables["merge_resolve_attempts"].count("mc-two") == 1

    @pytest.mark.asyncio
    async def test_merge_retry_counter_ignores_retry_later_tool_results(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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

        response = await engine.evaluate(before_event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"

        await engine.evaluate(
            after_event({"success": False, "retry_later": True}),
            session_id=SESSION_ID,
            variables=variables,
        )
        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.variables["merge_resolve_attempts"].count("mc-one") == 0

        for expected_count, output in (
            (1, {"success": False, "error": "AI resolution failed"}),
            (2, {"success": True}),
            (3, {"success": False, "error": "ReadTimeout: (no message)"}),
        ):
            response = await engine.evaluate(
                before_event,
                session_id=SESSION_ID,
                variables=variables,
            )
            assert response.decision == "allow"
            await engine.evaluate(
                after_event(output),
                session_id=SESSION_ID,
                variables=variables,
            )
            instance = instance_mgr.get_for_session(SESSION_ID)
            assert instance is not None
            assert instance.variables["merge_resolve_attempts"].count("mc-one") == expected_count

        response = await engine.evaluate(before_event, session_id=SESSION_ID, variables=variables)

        assert response.decision == "block"
        assert response.reason is not None
        assert "retry cap reached" in response.reason
        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.variables["merge_resolve_attempts"].count("mc-one") == 3

    @pytest.mark.asyncio
    async def test_duplicate_proxy_before_tool_does_not_consume_retry_budget(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
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
                session_id=SESSION_ID,
                variables=variables,
            )
            assert response.decision == "allow"

            duplicate_response = await engine.evaluate(
                _make_event(
                    data=event_data,
                    metadata={"_mcp_proxy_duplicate_before_tool": True},
                ),
                session_id=SESSION_ID,
                variables=variables,
            )
            assert duplicate_response.decision == "allow"

            instance = instance_mgr.get_for_session(SESSION_ID)
            assert instance is not None
            assert instance.variables["merge_resolve_attempts"].count("mc-one") == expected_count

        response = await engine.evaluate(
            _make_event(data=event_data),
            session_id=SESSION_ID,
            variables=variables,
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert "retry cap reached" in response.reason


def _end_agent_run_workflow() -> dict[str, Any]:
    """Developer workflow whose implement step also blocks end_agent_run."""
    workflow_data = cast(dict[str, Any], json.loads(json.dumps(_DEVELOPER_WORKFLOW)))
    workflow_data["steps"][1]["blocked_mcp_tools"].append("gobby-agents:end_agent_run")
    return workflow_data


def _running_rule_engine_with_bound_task(
    db: "HubDatabase", *, task_closed: bool
) -> tuple[RuleEngine, LocalAgentRunManager, str]:
    """Rule engine whose active run is bound to a task, optionally closed."""
    from gobby.storage.tasks import LocalTaskManager

    task = LocalTaskManager(db).create_task(
        project_id=PROJECT_ID,
        title="Bound leaf for end_agent_run valve",
        validation_criteria="Enforcement valve test fixture.",
    )
    if task_closed:
        db.execute(
            "UPDATE tasks SET closed_at = CURRENT_TIMESTAMP WHERE id = %s",
            (task.id,),
        )
    run_manager = LocalAgentRunManager(db)
    run = run_manager.create(
        parent_session_id=SESSION_ID,
        child_session_id=SESSION_ID,
        provider="claude",
        prompt="test",
        task_id=task.id,
    )
    assert run_manager.start(run.id) is not None
    runner = MagicMock()
    runner.run_storage = run_manager
    return RuleEngine(db, runner=runner), run_manager, run.id


def _end_agent_run_event() -> HookEvent:
    return _make_event(
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-agents",
                "tool_name": "end_agent_run",
                "arguments": {},
            },
        }
    )


@pytest.mark.asyncio
async def test_end_agent_run_allowed_when_bound_task_terminal(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    """A run bound to a closed task may end itself despite a step block (#19554)."""
    _setup_step_workflow(
        db,
        manager,
        instance_mgr,
        current_step="implement",
        workflow_data=_end_agent_run_workflow(),
    )
    rule_engine, _run_manager, _run_id = _running_rule_engine_with_bound_task(db, task_closed=True)

    response = await rule_engine.evaluate(
        _end_agent_run_event(), session_id=SESSION_ID, variables={}
    )

    assert response.decision != "block"


@pytest.mark.asyncio
async def test_end_agent_run_still_blocked_when_bound_task_open(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    """The terminal-task valve must not weaken enforcement for open tasks."""
    _setup_step_workflow(
        db,
        manager,
        instance_mgr,
        current_step="implement",
        workflow_data=_end_agent_run_workflow(),
    )
    rule_engine, _run_manager, _run_id = _running_rule_engine_with_bound_task(db, task_closed=False)

    response = await rule_engine.evaluate(
        _end_agent_run_event(), session_id=SESSION_ID, variables={}
    )

    assert response.decision == "block"
    assert response.reason is not None
    assert "end_agent_run" in response.reason


@pytest.mark.asyncio
async def test_end_agent_run_blocked_when_run_has_no_bound_task(
    db: "HubDatabase",
    manager: AgentDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
) -> None:
    """A run without a bound task gets no valve; the step block stands."""
    _setup_step_workflow(
        db,
        manager,
        instance_mgr,
        current_step="implement",
        workflow_data=_end_agent_run_workflow(),
    )
    rule_engine, _run_manager, _run_id = _running_rule_engine(db)

    response = await rule_engine.evaluate(
        _end_agent_run_event(), session_id=SESSION_ID, variables={}
    )

    assert response.decision == "block"


@pytest.mark.parametrize(
    ("spelling", "canonical"),
    [
        ("mcp__gobby__call_tool", "mcp__gobby__call_tool"),
        ("mcp_gobby_call_tool", "mcp__gobby__call_tool"),
        ("gobby__call_tool", "mcp__gobby__call_tool"),
        ("call_tool", "mcp__gobby__call_tool"),
        ("gobby__get_tool_schema", "mcp__gobby__get_tool_schema"),
        ("mcp_gobby_set_variable", "mcp__gobby__set_variable"),
        ("set_variable", "mcp__gobby__set_variable"),
        ("Bash", "Bash"),
        ("mcp__context7__get-library-docs", "mcp__context7__get-library-docs"),
        ("gobby__", "gobby__"),
        ("gobby-tasks__get_task", "gobby-tasks__get_task"),
    ],
)
@pytest.mark.unit
def test_canonical_gobby_tool_name(spelling: str, canonical: str) -> None:
    assert canonical_gobby_tool_name(spelling) == canonical


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("gobby__call_tool", True),
        ("mcp_gobby_call_tool", True),
        ("call_tool", True),
        ("mcp__gobby__call_tool", True),
        ("gobby__list_tools", False),
        ("Bash", False),
        ("", False),
        (None, False),
    ],
)
@pytest.mark.unit
def test_is_gobby_call_tool(spelling: str | None, expected: bool) -> None:
    assert is_gobby_call_tool(spelling) is expected


@pytest.mark.integration
class TestProviderToolNameNormalization:
    """Grok and other runtimes emit Gobby proxy tools without the mcp__ prefix.

    Enforcement must compare canonical spellings; exact-match killed four
    plan-adversary runs in load_skill (rule step-native-tool-allowlist).
    """

    @pytest.mark.asyncio
    async def test_grok_call_tool_alias_passes_step_allowlist(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """gobby__call_tool passes an allowlist authored as mcp__gobby__call_tool."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            data={
                "tool_name": "gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "claim_task",
                },
            }
        )

        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_grok_get_tool_schema_alias_passes_discovery_exemption(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """gobby__get_tool_schema is discovery-exempt even when absent from the allowlist."""
        workflow = {
            "name": "call-tool-only-workflow",
            "version": "1.0",
            "enabled": False,
            "variables": {},
            "steps": [
                {
                    "name": "claim",
                    "allowed_tools": ["mcp__gobby__call_tool"],
                    "allowed_mcp_tools": ["gobby-tasks:claim_task"],
                }
            ],
            "exit_condition": "false",
        }
        _setup_step_workflow(
            db, manager, instance_mgr, current_step="claim", workflow_data=workflow
        )
        event = _make_event(data={"tool_name": "gobby__get_tool_schema"})

        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_grok_native_search_tool_still_blocked(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """Canonicalization does not fail open: foreign native tools stay denied."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(data={"tool_name": "search_tool"})

        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})
        assert response.decision == "block"
        assert response.reason is not None
        assert "step-enforcement" in response.reason

    @pytest.mark.asyncio
    async def test_grok_call_tool_alias_respects_mcp_allowlist(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """gobby__call_tool reaches the MCP allow-list check instead of skipping it."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="claim")
        event = _make_event(
            data={
                "tool_name": "gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-agents",
                    "tool_name": "spawn_agent",
                },
            }
        )

        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})
        assert response.decision == "block"
        assert response.reason is not None
        assert "gobby-agents:spawn_agent" in response.reason

    @pytest.mark.asyncio
    async def test_implement_to_terminate_transition_for_grok_call_tool_alias(
        self,
        db: "HubDatabase",
        manager: AgentDefinitionManager,
        engine: RuleEngine,
        instance_mgr: AgentStepInstanceManager,
    ) -> None:
        """on_mcp_success handlers fire for gobby__call_tool so steps still advance."""
        _setup_step_workflow(db, manager, instance_mgr, current_step="implement")
        event = _make_event(
            event_type=HookEventType.AFTER_TOOL,
            data={
                "tool_name": "gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks-ops",
                    "tool_name": "submit_for_review",
                },
            },
        )

        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

        instance = instance_mgr.get_for_session(SESSION_ID)
        assert instance is not None
        assert instance.current_step == "terminate"
        assert instance.variables.get("review_submitted") is True
        assert response.context is not None
        assert "terminate" in response.context
