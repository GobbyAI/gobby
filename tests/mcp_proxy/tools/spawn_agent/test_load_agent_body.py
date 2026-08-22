"""Tests for loading spawn-agent definitions from rule_definitions."""

from __future__ import annotations

import pytest

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody, AgentWorkflows

pytestmark = pytest.mark.unit


class TestLoadAgentBody:
    """_load_agent_body loads from rule_definitions."""

    def test_loads_existing_agent(self, db: HubDatabase, manager: AgentDefinitionManager) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body

        body = AgentDefinitionBody(
            prompts={"agent": "Write clean code."},
            name="test-dev-load",
            description="Developer agent",
            provider="claude",
            model="claude-sonnet-4-6",
            isolation="worktree",
            base_branch="main",
            timeout=120.0,
            workflows=AgentWorkflows(rules=["require-task-before-edit", "require-commit"]),
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
        assert result.isolation == "worktree"
        assert result.workflows.rules == ["require-task-before-edit", "require-commit"]

    def test_returns_none_for_missing_agent(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body

        result = _load_agent_body("nonexistent-agent", db)
        assert result is None

    def test_returns_none_for_none_db(self) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body

        result = _load_agent_body("any-agent", None)
        assert result is None

    def test_ignores_non_agent_types(self, db: HubDatabase) -> None:
        from gobby.mcp_proxy.tools.spawn_agent._factory import _load_agent_body
        from gobby.storage.definitions.rules import RuleDefinitionManager

        RuleDefinitionManager(db).create(
            name="test-rule-not-agent",
            definition_json={"event": "before_tool", "effect": {"type": "block"}},
        )

        result = _load_agent_body("test-rule-not-agent", db)
        assert result is None
