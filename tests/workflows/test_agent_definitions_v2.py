"""Tests for AgentDefinitionBody, AgentWorkflows, and agent_scope on RuleDefinitionBody.

Covers:
- AgentDefinitionBody model (current expanded field set including surfaces)
- AgentWorkflows model (pipeline, rules, variables)
- agent_scope field on RuleDefinitionBody (list[str] | None)
- Serialization to/from agent_definitions
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> AgentDefinitionManager:
    return AgentDefinitionManager(db)


@pytest.fixture
def rule_manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


# ═══════════════════════════════════════════════════════════════════════
# AgentDefinitionBody model
# ═══════════════════════════════════════════════════════════════════════


class TestAgentDefinitionBodyModel:
    """AgentDefinitionBody has the current field set with correct defaults."""

    def test_minimal_creation(self) -> None:
        """Create with a name and the required default-surface prompt."""
        from gobby.workflows.definitions import AgentDefinitionBody

        body = AgentDefinitionBody(name="developer", prompts={"agent": "Develop the task."})
        assert body.name == "developer"
        assert body.description is None
        assert body.surfaces == ["spawn"]
        assert body.prompts.agent == "Develop the task."
        assert body.prompts.persona is None
        assert body.provider == "inherit"
        assert body.model is None
        assert body.api_base is None
        assert body.api_token is None
        assert body.isolation == "inherit"
        assert body.base_branch == "inherit"
        assert body.timeout == 0
        assert body.workflows.rules == []
        assert body.workflows.pipeline is None
        assert body.workflows.variables == {}
        assert body.step_workflow is None
        assert body.enabled is True

    def test_full_creation(self) -> None:
        """Create with all fields specified."""
        from gobby.workflows.definitions import AgentDefinitionBody, AgentWorkflows

        body = AgentDefinitionBody(
            name="qa",
            description="QA agent for testing",
            surfaces=["spawn", "persona"],
            prompts={
                "persona": "Help assess code quality interactively.",
                "agent": "Review the assigned implementation and report findings.",
            },
            provider="codex",
            model="gpt-5.4",
            isolation="worktree",
            base_branch="develop",
            timeout=300.0,
            workflows=AgentWorkflows(rules=["no-code-writing", "require-tests"]),
            enabled=False,
        )
        assert body.name == "qa"
        assert body.description == "QA agent for testing"
        assert body.prompt_for("persona") == "Help assess code quality interactively."
        assert body.prompt_for("agent") == (
            "Review the assigned implementation and report findings."
        )
        assert body.provider == "codex"
        assert body.model == "gpt-5.4"
        assert body.isolation == "worktree"
        assert body.base_branch == "develop"
        assert body.timeout == 300.0
        assert body.workflows.rules == ["no-code-writing", "require-tests"]
        assert body.enabled is False

    def test_field_count(self) -> None:
        """AgentDefinitionBody exposes the current expanded field set."""
        from gobby.workflows.definitions import AgentDefinitionBody, AgentStepWorkflowBody

        fields = AgentDefinitionBody.model_fields
        assert len(fields) == 21, f"Expected 21 fields, got {len(fields)}: {list(fields.keys())}"
        assert "surfaces" in fields
        assert "prompts" in fields
        assert "reasoning_required" in fields
        assert "fallback_agent" in fields
        assert "max_turns" not in fields
        assert "steps" not in fields
        assert "step_variables" not in fields
        assert "exit_condition" not in fields
        assert "step_workflow" in fields
        nested = AgentStepWorkflowBody.model_fields
        assert "steps" in nested
        assert "variables" in nested
        assert "exit_condition" in nested

    def test_surfaces_normalize_and_deduplicate(self) -> None:
        """Persona/spawn usage surfaces normalize from YAML-ish inputs."""
        from gobby.workflows.definitions import AgentDefinitionBody

        body = AgentDefinitionBody(
            name="planner",
            surfaces=["persona", "spawn", "persona"],
            prompts={"persona": "Plan interactively.", "agent": "Plan the assigned work."},
        )
        assert body.surfaces == ["persona", "spawn"]

        body = AgentDefinitionBody(
            name="planner",
            surfaces="persona",
            prompts={"persona": "Plan interactively."},
        )
        assert body.surfaces == ["persona"]

    def test_prompt_for_rejects_unsupported_surface(self) -> None:
        from gobby.workflows.definitions import AgentDefinitionBody

        persona = AgentDefinitionBody(
            name="comms",
            surfaces=["persona"],
            prompts={"persona": "Coordinate interactively."},
        )

        with pytest.raises(ValueError, match="'spawn' surface"):
            persona.prompt_for("agent")

    @pytest.mark.parametrize(
        ("surfaces", "prompts", "missing_block"),
        [
            (["persona"], {}, "prompts.persona"),
            (["spawn"], {}, "prompts.agent"),
            (["spawn", "persona"], {"agent": "Run the task."}, "prompts.persona"),
            (["spawn", "persona"], {"persona": "Guide the user."}, "prompts.agent"),
        ],
    )
    def test_declared_surfaces_require_non_empty_prompt_blocks(
        self,
        surfaces: list[str],
        prompts: dict[str, str],
        missing_block: str,
    ) -> None:
        from gobby.workflows.definitions import AgentDefinitionBody

        with pytest.raises(ValidationError, match=missing_block):
            AgentDefinitionBody(name="invalid", surfaces=surfaces, prompts=prompts)

    @pytest.mark.parametrize("legacy_field", ["role", "goal", "personality", "instructions"])
    def test_legacy_prompt_fields_are_rejected_with_migration_hint(
        self,
        legacy_field: str,
    ) -> None:
        from gobby.workflows.definitions import AgentDefinitionBody

        with pytest.raises(ValidationError, match="prompts.persona"):
            AgentDefinitionBody.model_validate(
                {
                    "name": "legacy",
                    "prompts": {"agent": "Run the task."},
                    legacy_field: "legacy content",
                }
            )

    def test_workflows_default_empty(self) -> None:
        """Workflows defaults to empty AgentWorkflows."""
        from gobby.workflows.definitions import AgentDefinitionBody

        body = AgentDefinitionBody(name="test", prompts={"agent": "Run the task."})
        assert body.workflows.rules == []
        assert body.workflows.pipeline is None
        assert body.workflows.variables == {}
        assert isinstance(body.workflows.rules, list)

    def test_isolation_values(self) -> None:
        """Isolation accepts none, worktree, clone, or None."""
        from gobby.workflows.definitions import AgentDefinitionBody

        for iso in ("none", "worktree", "clone"):
            body = AgentDefinitionBody(
                name="test", prompts={"agent": "Run the task."}, isolation=iso
            )
            assert body.isolation == iso

    def test_api_base_and_token(self) -> None:
        """api_base and api_token configure local model endpoints."""
        from gobby.workflows.definitions import AgentDefinitionBody

        body = AgentDefinitionBody(
            name="local-dev",
            prompts={"agent": "Run the task."},
            model="qwen3-8b",
            api_base="http://localhost:1234/v1",
            api_token="sk-local",
        )
        assert body.api_base == "http://localhost:1234/v1"
        assert body.api_token == "sk-local"

    def test_api_token_env_var_pattern(self) -> None:
        """api_token accepts ${ENV_VAR} pattern for env var expansion."""
        from gobby.workflows.definitions import AgentDefinitionBody

        body = AgentDefinitionBody(
            name="local-dev",
            prompts={"agent": "Run the task."},
            api_token="${MY_API_KEY}",
        )
        assert body.api_token == "${MY_API_KEY}"

        body = AgentDefinitionBody(name="test", prompts={"agent": "Run the task."})
        assert body.isolation == "inherit"

    def test_reasoning_effort_normalizes_string_values(self) -> None:
        """reasoning_effort keeps string normalization while rejecting coercion."""
        from gobby.workflows.definitions import AgentDefinitionBody

        body = AgentDefinitionBody(
            name="planner", prompts={"agent": "Run the task."}, reasoning_effort=" High "
        )
        assert body.reasoning_effort == "high"

    def test_reasoning_effort_rejects_non_string_values(self) -> None:
        """reasoning_effort should fail early on malformed YAML types."""
        from gobby.workflows.definitions import AgentDefinitionBody

        with pytest.raises(ValidationError, match="reasoning_effort"):
            AgentDefinitionBody(name="planner", reasoning_effort=1)

    def test_reasoning_required_rejects_non_bool_values(self) -> None:
        """reasoning_required should stay strict instead of coercing strings."""
        from gobby.workflows.definitions import AgentDefinitionBody

        with pytest.raises(ValidationError, match="reasoning_required"):
            AgentDefinitionBody(name="planner", reasoning_required="true")

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("model", 123),
            ("fallback_agent", 123),
            ("api_base", 123),
            ("api_token", 123),
        ],
    )
    def test_execution_string_fields_reject_non_string_values(
        self,
        field_name: str,
        value: int,
    ) -> None:
        """Execution config string fields should not stringify malformed values."""
        from gobby.workflows.definitions import AgentDefinitionBody

        with pytest.raises(ValidationError, match=field_name):
            AgentDefinitionBody(name="planner", **{field_name: value})


class TestAgentDefinitionBodySerialization:
    """AgentDefinitionBody serializes correctly to/from JSON."""

    def test_json_round_trip(self) -> None:
        """Serialize to JSON and back preserves all fields."""
        from gobby.workflows.definitions import AgentDefinitionBody, AgentWorkflows

        original = AgentDefinitionBody(
            name="developer",
            description="Writes code",
            prompts={"agent": "Write clean code."},
            provider="claude",
            model="claude-sonnet-4-6",
            isolation="worktree",
            base_branch="main",
            timeout=120.0,
            workflows=AgentWorkflows(rules=["require-task-before-edit", "require-commit"]),
            enabled=True,
        )

        json_str = original.model_dump_json()
        restored = AgentDefinitionBody.model_validate_json(json_str)

        assert restored.name == original.name
        assert restored.description == original.description
        assert restored.prompts == original.prompts
        assert restored.provider == original.provider
        assert restored.model == original.model
        assert restored.isolation == original.isolation
        assert restored.base_branch == original.base_branch
        assert restored.timeout == original.timeout
        assert restored.workflows.rules == original.workflows.rules
        assert restored.enabled == original.enabled

    def test_minimal_json_round_trip(self) -> None:
        """Minimal spawn agent serializes and deserializes."""
        from gobby.workflows.definitions import AgentDefinitionBody

        original = AgentDefinitionBody(name="simple", prompts={"agent": "Run the task."})
        json_str = original.model_dump_json()
        restored = AgentDefinitionBody.model_validate_json(json_str)
        assert restored.name == "simple"
        assert restored.workflows.rules == []


# ═══════════════════════════════════════════════════════════════════════
# agent_scope on RuleDefinitionBody
# ═══════════════════════════════════════════════════════════════════════


class TestAgentScopeOnRuleDefinitionBody:
    """RuleDefinitionBody has agent_scope field (list[str] | None)."""

    def test_agent_scope_default_none(self) -> None:
        """agent_scope defaults to None (global rule)."""
        body = RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="test")],
        )
        assert body.agent_scope is None

    def test_agent_scope_with_single_agent(self) -> None:
        """agent_scope can be set to a single agent name."""
        body = RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="test")],
            agent_scope=["developer"],
        )
        assert body.agent_scope == ["developer"]

    def test_agent_scope_with_multiple_agents(self) -> None:
        """agent_scope can include multiple agent names."""
        body = RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="test")],
            agent_scope=["developer", "qa"],
        )
        assert body.agent_scope == ["developer", "qa"]
        assert len(body.agent_scope) == 2

    def test_agent_scope_json_round_trip(self) -> None:
        """agent_scope survives JSON serialization."""
        body = RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="test")],
            agent_scope=["coordinator"],
            group="coordinator-agent",
        )
        json_str = body.model_dump_json()
        restored = RuleDefinitionBody.model_validate_json(json_str)
        assert restored.agent_scope == ["coordinator"]
        assert restored.group == "coordinator-agent"

    def test_agent_scope_none_not_in_json(self) -> None:
        """When agent_scope is None, it's excluded from JSON output (or set to null)."""
        body = RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="test")],
        )
        data = body.model_dump()
        # agent_scope should exist in the model but be None
        assert "agent_scope" in data
        assert data["agent_scope"] is None


# ═══════════════════════════════════════════════════════════════════════
# Storage: agent_definitions
# ═══════════════════════════════════════════════════════════════════════


class TestAgentDefinitionStorage:
    """Agent definitions stored in agent_definitions."""

    def _make_agent_json(self, **overrides: Any) -> str:
        from gobby.workflows.definitions import AgentDefinitionBody

        defaults: dict[str, Any] = {
            "name": "developer",
            "prompts": {"agent": "Run the task."},
        }
        defaults.update(overrides)
        body = AgentDefinitionBody(**defaults)
        return body.model_dump_json()

    def test_create_agent_definition(self, manager: AgentDefinitionManager) -> None:
        """Create an agent definition stored in agent_definitions."""
        row = manager.create(
            name="test-developer-agent",
            definition_json=self._make_agent_json(
                name="test-developer-agent",
                description="Writes code",
                prompts={"agent": "Write clean code."},
            ),
        )
        assert row.name == "test-developer-agent"
        assert row.definition_json is not None

    def test_round_trip_through_storage(self, manager: AgentDefinitionManager) -> None:
        """Store and retrieve agent definition, deserialize definition_json."""
        from gobby.workflows.definitions import AgentDefinitionBody, AgentWorkflows

        original = AgentDefinitionBody(
            name="qa",
            description="QA agent",
            prompts={"agent": "Test everything."},
            provider="codex",
            model="gpt-5.4",
            isolation="worktree",
            base_branch="develop",
            timeout=300.0,
            workflows=AgentWorkflows(rules=["no-code-writing"]),
            enabled=True,
        )

        row = manager.create(
            name=original.name,
            definition_json=original.model_dump_json(),
            description=original.description,
        )

        fetched = manager.get(row.id)
        restored = AgentDefinitionBody.model_validate(fetched.definition_json)

        assert restored.name == original.name
        assert restored.description == original.description
        assert restored.prompts == original.prompts
        assert restored.provider == original.provider
        assert restored.model == original.model
        assert restored.isolation == original.isolation
        assert restored.base_branch == original.base_branch
        assert restored.timeout == original.timeout
        assert restored.workflows.rules == original.workflows.rules
        assert restored.enabled == original.enabled

    def test_list_agents_only(
        self, manager: AgentDefinitionManager, rule_manager: RuleDefinitionManager
    ) -> None:
        """Agent list_all does not include rule definitions."""
        manager.create(
            name="test-agent-list",
            definition_json=self._make_agent_json(name="test-agent-list"),
        )
        rule_manager.create(
            name="test-rule-list",
            definition_json={"event": "before_tool", "effect": {"type": "block"}},
        )

        agents = manager.list_all()
        assert {agent.name for agent in agents} == {"test-agent-list"}
        assert agents[0].name == "test-agent-list"

    def test_soft_delete_agent(self, manager: AgentDefinitionManager) -> None:
        """Soft-deleted agents are excluded from default queries."""
        row = manager.create(
            name="to-delete",
            definition_json=self._make_agent_json(name="to-delete"),
        )
        manager.delete(row.id)

        agents = manager.list_all()
        names = [a.name for a in agents]
        assert "to-delete" not in names

    def test_get_agent_by_name(self, manager: AgentDefinitionManager) -> None:
        """Retrieve agent definition by name via get_by_name."""
        manager.create(
            name="test-coordinator-agent",
            definition_json=self._make_agent_json(
                name="test-coordinator-agent",
                description="Orchestrates work",
            ),
        )

        row = manager.get_by_name("test-coordinator-agent")
        assert row is not None

        from gobby.workflows.definitions import AgentDefinitionBody

        body = AgentDefinitionBody.model_validate(row.definition_json)
        assert body.name == "test-coordinator-agent"
        assert body.description == "Orchestrates work"


# ═══════════════════════════════════════════════════════════════════════
# agent_scope in storage round-trip
# ═══════════════════════════════════════════════════════════════════════


class TestAgentScopeStorage:
    """agent_scope on rules survives storage round-trip."""

    def test_rule_with_agent_scope_storage(self, rule_manager: RuleDefinitionManager) -> None:
        """Rule with agent_scope stores and retrieves correctly."""
        body = RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", tools=["Edit", "Write"], reason="QA no code")],
            agent_scope=["qa"],
            group="qa-agent",
        )

        row = rule_manager.create(
            name="no-code-writing",
            definition_json=body.model_dump(),
        )

        fetched = rule_manager.get(row.id)
        restored = RuleDefinitionBody.model_validate(fetched.definition_json)
        assert restored.agent_scope == ["qa"]
        assert restored.group == "qa-agent"
        assert restored.effects[0].type == "block"

    def test_rule_without_agent_scope_storage(self, rule_manager: RuleDefinitionManager) -> None:
        """Rule without agent_scope (global) stores and retrieves correctly."""
        body = RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="Global rule")],
        )

        row = rule_manager.create(
            name="global-rule",
            definition_json=body.model_dump(),
        )

        fetched = rule_manager.get(row.id)
        restored = RuleDefinitionBody.model_validate(fetched.definition_json)
        assert restored.agent_scope is None
