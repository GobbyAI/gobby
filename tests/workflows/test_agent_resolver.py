"""Tests for agent_resolver.resolve_agent()."""

from __future__ import annotations

import json
import logging

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.agent_resolver import resolve_agent
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


class TestResolveAgentDefault:
    """resolve_agent('default', db) returns Pydantic defaults when no DB record exists."""

    def test_default_returns_pydantic_defaults_when_no_db_record(self, db: HubDatabase) -> None:
        """When no 'default' agent is in the DB, resolve_agent returns AgentDefinitionBody defaults."""
        result = resolve_agent("default", db)
        assert result is not None
        assert isinstance(result, AgentDefinitionBody)
        assert result.name == "default"
        assert result.provider == "inherit"

    def test_default_uses_db_record_when_present(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When a 'default' agent exists in the DB, resolve_agent returns it instead of Pydantic defaults."""
        body = {"name": "default", "role": "custom default role", "provider": "claude"}
        manager.create(
            name="default",
            workflow_type="agent",
            definition_json=json.dumps(body),
            source="test",
        )

        result = resolve_agent("default", db)
        assert result is not None
        assert result.name == "default"
        assert result.role == "custom default role"
        assert result.provider == "claude"

    def test_nonexistent_agent_returns_none(self, db: HubDatabase) -> None:
        """A non-default agent that doesn't exist returns None."""
        result = resolve_agent("nonexistent", db)
        assert result is None


class TestResolveAgentLookup:
    """resolve_agent does direct DB lookup."""

    def test_simple_lookup(self, db: HubDatabase, manager: LocalWorkflowDefinitionManager) -> None:
        """Direct lookup returns the agent definition."""
        body = {
            "name": "developer",
            "role": "Backend developer",
            "provider": "claude",
        }
        manager.create(
            name="developer",
            workflow_type="agent",
            definition_json=json.dumps(body),
            source="test",
        )
        result = resolve_agent("developer", db)
        assert result is not None
        assert result.name == "developer"
        assert result.role == "Backend developer"
        assert result.provider == "claude"

    def test_skips_non_agent_type(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """A row with workflow_type != 'agent' is ignored."""
        manager.create(
            name="my-rule",
            workflow_type="rule",
            definition_json='{"event": "before_tool", "effect": {"type": "block", "reason": "no"}}',
            source="test",
        )
        result = resolve_agent("my-rule", db)
        assert result is None

    def test_invalid_agent_definition_logs_warning_with_traceback(
        self,
        db: HubDatabase,
        manager: LocalWorkflowDefinitionManager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Invalid persisted agent definitions log a warning with traceback context."""
        manager.create(
            name="broken-agent",
            workflow_type="agent",
            definition_json=json.dumps({"name": "broken-agent", "surfaces": [123]}),
            source="test",
        )

        with caplog.at_level(logging.WARNING, logger="gobby.workflows.agent_resolver"):
            result = resolve_agent("broken-agent", db)

        assert result is None
        assert "Failed to parse agent definition for broken-agent" in caplog.text
        assert any(record.exc_info for record in caplog.records)


class TestProviderNormalization:
    """Provider 'inherit' is resolved based on cli_source."""

    def test_inherit_resolved_to_claude_by_default(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        body = {"name": "test", "provider": "inherit"}
        manager.create(
            name="test",
            workflow_type="agent",
            definition_json=json.dumps(body),
            source="test",
        )
        result = resolve_agent("test", db)
        assert result is not None
        assert result.provider == "claude"

    def test_inherit_resolved_from_cli_source(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        body = {"name": "test2", "provider": "inherit"}
        manager.create(
            name="test2",
            workflow_type="agent",
            definition_json=json.dumps(body),
            source="test",
        )
        result = resolve_agent("test2", db, cli_source="gemini")
        assert result is not None
        assert result.provider == "gemini"

    def test_claude_source_maps_to_claude(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        body = {"name": "test3", "provider": "inherit"}
        manager.create(
            name="test3",
            workflow_type="agent",
            definition_json=json.dumps(body),
            source="test",
        )
        result = resolve_agent("test3", db, cli_source="claude")
        assert result is not None
        assert result.provider == "claude"

    def test_explicit_provider_not_overridden(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        body = {"name": "test4", "provider": "gemini"}
        manager.create(
            name="test4",
            workflow_type="agent",
            definition_json=json.dumps(body),
            source="test",
        )
        result = resolve_agent("test4", db, cli_source="claude")
        assert result is not None
        assert result.provider == "gemini"


def test_resolve_memory_recall_helper(db: HubDatabase) -> None:
    """The resolver loads the bundled memory recall helper by name."""
    sync_result = sync_bundled_agents(db)

    assert sync_result["success"] is True
    assert sync_result["errors"] == []

    result = resolve_agent("memory-recall-helper", db)

    assert result is not None
    assert result.model == "claude-haiku-4-5"
    assert result.max_turns == 3
    assert result.timeout == 60
    assert "mcp__gobby__set_variable" in result.blocked_tools
    assert result.blocked_mcp_tools == []
