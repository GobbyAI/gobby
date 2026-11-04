"""Tests for loading spawn-agent definitions from rule_definitions."""

from __future__ import annotations

import pytest

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from tests.fixtures.agent_definitions import make_agent_definition, make_agent_workflows

pytestmark = pytest.mark.unit


class TestLoadAgentBody:
    """_load_agent_body loads from rule_definitions."""

    def test_loads_existing_agent(self, db: HubDatabase, manager: AgentDefinitionManager) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body

        body = make_agent_definition(
            prompts={"agent": "Write clean code."},
            name="test-dev-load",
            description="Developer agent",
            provider="claude",
            model="claude-sonnet-4-6",
            checkout_mode="worktree",
            base_branch="main",
            timeout=120.0,
            workflows=make_agent_workflows(rules=["fixture-definition-55b74807", "require-commit"]),
        )
        manager.create(
            name=body.name,
            definition_json=body.model_dump_json(),
            description=body.description,
            enabled=True,
        )

        result = _load_agent_body("test-dev-load", db)
        assert result is not None
        assert result.name == "test-dev-load"
        assert result.provider == "claude"
        assert result.model == "claude-sonnet-4-6"
        assert result.checkout_mode == "worktree"
        assert result.workflows.rules == ["fixture-definition-55b74807", "require-commit"]

    def test_returns_none_for_missing_agent(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body

        result = _load_agent_body("nonexistent-agent", db)
        assert result is None

    @pytest.mark.parametrize("enabled", [True, False])
    def test_refuses_disabled_named_agent(
        self, db: HubDatabase, manager: AgentDefinitionManager, enabled: bool
    ) -> None:
        """Spawn never binds a seat to a disabled definition; it would fail closed on use."""
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body
        from gobby.workflows.agent_resolver import AgentResolutionError

        body = make_agent_definition(prompts={"agent": "Run the task."}, name="toggled-seat")
        row = manager.create(name=body.name, definition_json=body.model_dump_json())
        manager.update(row.id, enabled=enabled)

        if enabled:
            result = _load_agent_body("toggled-seat", db)
            assert result is not None
            assert result.name == "toggled-seat"
        else:
            with pytest.raises(AgentResolutionError, match="'toggled-seat' is disabled"):
                _load_agent_body("toggled-seat", db)

    def test_returns_none_for_none_db(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body

        result = _load_agent_body("any-agent", None)
        assert result is None

    def test_ignores_non_agent_types(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body
        from gobby.storage.definitions.rules import RuleDefinitionManager

        RuleDefinitionManager(db).create(
            name="test-rule-not-agent",
            definition_json={
                "event": "before_tool",
                "effects": [{"type": "block", "reason": "Fixture refusal"}],
            },
        )

        result = _load_agent_body("test-rule-not-agent", db)
        assert result is None
