"""Tests for agent definition CRUD tools."""

from __future__ import annotations

import json

import pytest

from gobby.mcp_proxy.tools.workflows._agents import (
    create_agent_definition,
    delete_agent_definition,
    get_agent_definition,
    list_agent_definitions,
    toggle_agent_definition,
    update_agent_step_workflow,
)
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.workflows.definitions import AgentDefinitionBody

pytest_plugins = ["tests.storage.definitions.conftest"]
pytestmark = pytest.mark.unit


def _setup(db: PostgresHubDatabase) -> AgentDefinitionManager:
    return AgentDefinitionManager(db)


def _insert_agent(
    mgr: AgentDefinitionManager,
    name: str = "test-agent",
    source: str = "installed",
    enabled: bool = True,
    tags: list[str] | None = None,
    **overrides: object,
) -> None:
    body = AgentDefinitionBody(name=name, enabled=enabled, **overrides)
    dumped = body.model_dump(mode="json")
    mgr.upsert_with_steps(
        name,
        dumped,
        dumped.get("step_workflow"),
        description=body.description,
        source=source,
        enabled=enabled,
        tags=tags,
    )


class TestListAgentDefinitions:
    def test_empty(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        result = list_agent_definitions(mgr)
        assert result["success"] is True
        assert result["count"] == 0
        assert result["agents"] == []

    def test_with_agents(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "alpha", description="Agent A")
        _insert_agent(mgr, "beta", description="Agent B")
        result = list_agent_definitions(mgr)
        assert result["success"] is True
        assert result["count"] == 2
        names = [a["name"] for a in result["agents"]]
        assert "alpha" in names
        assert "beta" in names

    def test_filter_enabled(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "enabled-agent", enabled=True)
        _insert_agent(mgr, "disabled-agent", enabled=False)
        result = list_agent_definitions(mgr, enabled=True)
        assert result["count"] == 1
        assert result["agents"][0]["name"] == "enabled-agent"

    def test_summary_fields(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "summary-test", provider="codex", surfaces=["spawn", "persona"])
        result = list_agent_definitions(mgr)
        agent = result["agents"][0]
        assert agent["provider"] == "codex"
        assert agent["source"] == "installed"
        assert agent["surfaces"] == ["spawn", "persona"]

    def test_filter_surface(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "spawn-only", surfaces=["spawn"])
        _insert_agent(mgr, "persona-ready", surfaces=["spawn", "persona"])

        result = list_agent_definitions(mgr, surface_filter="persona")

        assert result["count"] == 1
        assert result["agents"][0]["name"] == "persona-ready"


class TestGetAgentDefinition:
    def test_found(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "worker", description="A worker", provider="claude")
        result = get_agent_definition(mgr, "worker")
        assert result["success"] is True
        agent = result["agent"]
        assert agent["name"] == "worker"
        assert agent["description"] == "A worker"
        assert agent["provider"] == "claude"

    def test_not_found(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        result = get_agent_definition(mgr, "nonexistent")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_detail_fields(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(
            mgr,
            "detailed",
            role="tester",
            goal="test things",
            personality="calm",
            instructions="read first",
            timeout=300.0,
        )
        result = get_agent_definition(mgr, "detailed")
        agent = result["agent"]
        assert agent["role"] == "tester"
        assert agent["goal"] == "test things"
        assert agent["personality"] == "calm"
        assert agent["instructions"] == "read first"
        assert agent["timeout"] == 300.0
        assert "max_turns" not in agent

    def test_detail_includes_nested_step_workflow(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(
            mgr,
            "stepful",
            step_workflow={
                "variables": {"goal": "ship"},
                "exit_condition": "done",
                "steps": [{"name": "implement"}],
            },
        )
        result = get_agent_definition(mgr, "stepful")
        assert result["success"] is True
        nested = result["agent"]["step_workflow"]
        assert nested["steps"][0]["name"] == "implement"
        assert "steps" not in result["agent"]


class TestCreateAgentDefinition:
    def test_basic(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        result = create_agent_definition(mgr, "new-agent", {"provider": "claude"})
        assert result["success"] is True
        assert result["agent"]["name"] == "new-agent"

    def test_with_all_fields(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        result = create_agent_definition(
            mgr,
            "full-agent",
            {
                "description": "Full agent",
                "role": "dev",
                "goal": "build things",
                "provider": "codex",
                "model": "gpt-5.4",
                "timeout": 300.0,
            },
        )
        assert result["success"] is True
        assert result["agent"]["provider"] == "codex"

    def test_stale_max_turns_input_is_not_persisted(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        mgr = _setup(definition_db)
        result = create_agent_definition(
            mgr,
            "stale-limit-agent",
            {"description": "Old payload", "max_turns": 20},
        )
        row = mgr.get_by_name("stale-limit-agent")

        assert result["success"] is True
        assert "max_turns" not in result["agent"]
        assert row is not None
        persisted = (
            json.loads(row.definition_json)
            if isinstance(row.definition_json, str)
            else row.definition_json
        )
        assert "max_turns" not in persisted

    def test_duplicate_fails(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        create_agent_definition(mgr, "dup", {})
        result = create_agent_definition(mgr, "dup", {})
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_invalid_definition(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        result = create_agent_definition(mgr, "bad", {"isolation": "invalid_isolation"})
        assert result["success"] is False
        assert "Validation failed" in result["error"]

    def test_persists_to_db(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        create_agent_definition(mgr, "persistent", {"description": "Stays in DB"})
        # Verify via list
        result = list_agent_definitions(mgr)
        assert any(a["name"] == "persistent" for a in result["agents"])


class TestToggleAgentDefinition:
    def test_disable(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "toggle-me")
        result = toggle_agent_definition(mgr, "toggle-me", enabled=False)
        assert result["success"] is True
        assert result["agent"]["enabled"] is False

    def test_enable(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "toggle-me", enabled=False)
        result = toggle_agent_definition(mgr, "toggle-me", enabled=True)
        assert result["success"] is True
        assert result["agent"]["enabled"] is True

    def test_not_found(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        result = toggle_agent_definition(mgr, "nonexistent", enabled=True)
        assert result["success"] is False
        assert "not found" in result["error"]


class TestDeleteAgentDefinition:
    def test_delete_user_created(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "deletable", tags=["user"])
        result = delete_agent_definition(mgr, "deletable")
        assert result["success"] is True
        assert result["deleted"]["name"] == "deletable"

    def test_delete_not_found(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        result = delete_agent_definition(mgr, "nonexistent")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_bundled_protected(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "bundled-agent", tags=["gobby"])
        result = delete_agent_definition(mgr, "bundled-agent")
        assert result["success"] is False
        assert "bundled" in result["error"]

    def test_bundled_force_delete(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "bundled-agent", tags=["gobby"])
        result = delete_agent_definition(mgr, "bundled-agent", force=True)
        assert result["success"] is True

    def test_deleted_not_in_list(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "gone", tags=["user"])
        delete_agent_definition(mgr, "gone")
        result = list_agent_definitions(mgr)
        assert not any(a["name"] == "gone" for a in result["agents"])


class TestUpdateAgentStepWorkflow:
    def test_sets_and_clears_nested_workflow(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "coder")
        updated = update_agent_step_workflow(
            mgr,
            "coder",
            {
                "variables": {"goal": "ship"},
                "exit_condition": "done",
                "steps": [{"name": "implement"}],
            },
        )
        assert updated["success"] is True
        assert updated["step_count"] == 1
        detail = get_agent_definition(mgr, "coder")["agent"]
        assert detail["step_workflow"]["steps"][0]["name"] == "implement"
        cleared = update_agent_step_workflow(mgr, "coder", None)
        assert cleared["success"] is True
        assert get_agent_definition(mgr, "coder")["agent"]["step_workflow"] is None
