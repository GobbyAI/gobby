"""Tests for agent definition CRUD tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.workflows._agents import (
    create_agent_definition,
    delete_agent_definition,
    get_agent_definition,
    list_agent_definitions,
    toggle_agent_definition,
)
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody

pytestmark = pytest.mark.unit


def _setup(tmp_path: Path) -> LocalWorkflowDefinitionManager:
    db = LocalDatabase(tmp_path / "test.db")
    run_migrations(db)
    return LocalWorkflowDefinitionManager(db)


def _insert_agent(
    mgr: LocalWorkflowDefinitionManager,
    name: str = "test-agent",
    source: str = "installed",
    enabled: bool = True,
    **overrides: object,
) -> None:
    body = AgentDefinitionBody(name=name, enabled=enabled, **overrides)  # type: ignore[arg-type]
    mgr.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="agent",
        description=body.description,
        source=source,
        enabled=enabled,
    )


class TestListAgentDefinitions:
    def test_empty(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        result = list_agent_definitions(mgr)
        assert result["success"] is True
        assert result["count"] == 0
        assert result["agents"] == []

    def test_with_agents(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(mgr, "alpha", description="Agent A")
        _insert_agent(mgr, "beta", description="Agent B")
        result = list_agent_definitions(mgr)
        assert result["success"] is True
        assert result["count"] == 2
        names = [a["name"] for a in result["agents"]]
        assert "alpha" in names
        assert "beta" in names

    def test_filter_enabled(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(mgr, "enabled-agent", enabled=True)
        _insert_agent(mgr, "disabled-agent", enabled=False)
        result = list_agent_definitions(mgr, enabled=True)
        assert result["count"] == 1
        assert result["agents"][0]["name"] == "enabled-agent"

    def test_summary_fields(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(mgr, "summary-test", provider="gemini", mode="terminal")
        result = list_agent_definitions(mgr)
        agent = result["agents"][0]
        assert agent["provider"] == "gemini"
        assert agent["mode"] == "terminal"
        assert agent["source"] == "installed"


class TestGetAgentDefinition:
    def test_found(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(mgr, "worker", description="A worker", provider="claude", mode="autonomous")
        result = get_agent_definition(mgr, "worker")
        assert result["success"] is True
        agent = result["agent"]
        assert agent["name"] == "worker"
        assert agent["description"] == "A worker"
        assert agent["provider"] == "claude"
        assert agent["mode"] == "autonomous"

    def test_not_found(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        result = get_agent_definition(mgr, "nonexistent")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_detail_fields(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(
            mgr,
            "detailed",
            role="tester",
            goal="test things",
            personality="calm",
            instructions="read first",
            timeout=300.0,
            max_turns=20,
        )
        result = get_agent_definition(mgr, "detailed")
        agent = result["agent"]
        assert agent["role"] == "tester"
        assert agent["goal"] == "test things"
        assert agent["personality"] == "calm"
        assert agent["instructions"] == "read first"
        assert agent["timeout"] == 300.0
        assert agent["max_turns"] == 20

    def test_ignores_non_agent_type(self, tmp_path: Path) -> None:
        """get_agent_definition should not return rules even if name matches."""
        mgr = _setup(tmp_path)
        # Insert a rule, not an agent
        mgr.create(
            name="my-rule",
            definition_json='{"event": "before_tool", "effect": {"type": "block", "reason": "no"}}',
            workflow_type="rule",
            source="installed",
        )
        result = get_agent_definition(mgr, "my-rule")
        assert result["success"] is False


class TestCreateAgentDefinition:
    def test_basic(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        result = create_agent_definition(mgr, "new-agent", {"provider": "claude"})
        assert result["success"] is True
        assert result["agent"]["name"] == "new-agent"

    def test_with_all_fields(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        result = create_agent_definition(
            mgr,
            "full-agent",
            {
                "description": "Full agent",
                "role": "dev",
                "goal": "build things",
                "provider": "gemini",
                "model": "flash",
                "mode": "terminal",
                "timeout": 300.0,
                "max_turns": 20,
            },
        )
        assert result["success"] is True
        assert result["agent"]["provider"] == "gemini"

    def test_duplicate_fails(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        create_agent_definition(mgr, "dup", {})
        result = create_agent_definition(mgr, "dup", {})
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_invalid_definition(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        result = create_agent_definition(mgr, "bad", {"mode": "invalid_mode"})
        assert result["success"] is False
        assert "Validation failed" in result["error"]

    def test_persists_to_db(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        create_agent_definition(mgr, "persistent", {"description": "Stays in DB"})
        # Verify via list
        result = list_agent_definitions(mgr)
        assert any(a["name"] == "persistent" for a in result["agents"])


class TestToggleAgentDefinition:
    def test_disable(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(mgr, "toggle-me")
        result = toggle_agent_definition(mgr, "toggle-me", enabled=False)
        assert result["success"] is True
        assert result["agent"]["enabled"] is False

    def test_enable(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(mgr, "toggle-me", enabled=False)
        result = toggle_agent_definition(mgr, "toggle-me", enabled=True)
        assert result["success"] is True
        assert result["agent"]["enabled"] is True

    def test_not_found(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        result = toggle_agent_definition(mgr, "nonexistent", enabled=True)
        assert result["success"] is False
        assert "not found" in result["error"]


class TestDeleteAgentDefinition:
    def test_delete_installed(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(mgr, "deletable", source="installed")
        result = delete_agent_definition(mgr, "deletable")
        assert result["success"] is True
        assert result["deleted"]["name"] == "deletable"

    def test_delete_not_found(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        result = delete_agent_definition(mgr, "nonexistent")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_template_protected(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(mgr, "tpl-agent", source="template")
        result = delete_agent_definition(mgr, "tpl-agent")
        assert result["success"] is False
        assert "template" in result["error"]

    def test_template_force_delete(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(mgr, "tpl-agent", source="template")
        result = delete_agent_definition(mgr, "tpl-agent", force=True)
        assert result["success"] is True

    def test_deleted_not_in_list(self, tmp_path: Path) -> None:
        mgr = _setup(tmp_path)
        _insert_agent(mgr, "gone")
        delete_agent_definition(mgr, "gone")
        result = list_agent_definitions(mgr)
        assert not any(a["name"] == "gone" for a in result["agents"])
