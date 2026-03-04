"""Tests for agent_resolver.resolve_agent()."""

from __future__ import annotations

import json

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.agent_resolver import resolve_agent
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_agent_resolver.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


class TestResolveAgentDefault:
    """resolve_agent('default', db) returns Pydantic defaults when no DB record exists."""

    def test_default_returns_pydantic_defaults_when_no_db_record(self, db: LocalDatabase) -> None:
        """When no 'default' agent is in the DB, resolve_agent returns AgentDefinitionBody defaults."""
        result = resolve_agent("default", db)
        assert result is not None
        assert isinstance(result, AgentDefinitionBody)
        assert result.name == "default"
        assert result.provider == "inherit"
        assert result.extends is None

    def test_default_uses_db_record_when_present(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
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

    def test_nonexistent_agent_returns_none(self, db: LocalDatabase) -> None:
        """A non-default agent that doesn't exist returns None."""
        result = resolve_agent("nonexistent", db)
        assert result is None
