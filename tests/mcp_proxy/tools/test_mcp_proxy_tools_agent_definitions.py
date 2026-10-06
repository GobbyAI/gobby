"""Tests for agent definition CRUD tools."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from gobby.mcp_proxy.tools.workflows import create_workflows_registry
from gobby.mcp_proxy.tools.workflows._agents import (
    create_agent_definition,
    delete_agent_definition,
    get_agent_definition,
    list_agent_definitions,
    toggle_agent_definition,
    update_agent_step_workflow,
)
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions._shared import DefinitionSource
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.utils.local_token import AgentApiTokenClaims
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import reset_request_principal, set_request_principal
from tests.fixtures.agent_definitions import make_agent_definition

pytest_plugins = ["tests.storage.definitions.conftest"]
pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "create_agent_definition",
            {"name": "blocked", "definition": {"prompts": {"agent": "Run task."}}},
        ),
        ("toggle_agent_definition", {"name": "blocked", "enabled": False}),
        ("delete_agent_definition", {"name": "blocked", "force": True}),
        ("update_agent_rules", {"name": "blocked", "add": ["rule"]}),
        ("update_agent_variables", {"name": "blocked", "set_vars": {"key": "value"}}),
        ("update_agent_step_workflow", {"name": "blocked", "clear_step_workflow": True}),
        ("reload_cache", {}),
    ],
)
@pytest.mark.parametrize("rejected_principal", [False, True])
async def test_agent_token_cannot_mutate_definitions(
    tool_name: str, arguments: dict[str, object], rejected_principal: bool
) -> None:
    async def principal() -> AgentApiTokenClaims | Literal[False]:
        if rejected_principal:
            return False
        return AgentApiTokenClaims(
            session_id="agent-session",
            project_id="project",
            machine_id="machine",
            iat=1,
            exp=2,
        )

    token = set_request_principal(principal)
    try:
        registry = create_workflows_registry()
        result = await registry.call(tool_name, arguments)
    finally:
        reset_request_principal(token)

    assert result["success"] is False
    assert result["error_code"] == "forbidden"


async def test_operator_token_can_create_agent_definition(
    definition_db: PostgresHubDatabase,
) -> None:
    async def principal() -> None:
        return None

    token = set_request_principal(principal)
    try:
        registry = create_workflows_registry(db=definition_db)
        result = await registry.call(
            "create_agent_definition",
            {
                "name": "operator-agent",
                "definition": {
                    "provider": "claude",
                    "prompts": {"agent": "Run task."},
                    "workflows": {"rule_selectors": {"include": []}},
                },
            },
        )
    finally:
        reset_request_principal(token)

    assert result["success"] is True
    assert AgentDefinitionManager(definition_db).get_by_name("operator-agent") is not None


async def test_unseeded_reload_cache_syncs_bundled_agent(
    definition_db: PostgresHubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.workflows._import.sync_imported_workflows",
        lambda *_args, **_kwargs: {"synced": 0, "errors": []},
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.workflows._import._RELOAD_ONLY", frozenset({"agents"})
    )
    manager = AgentDefinitionManager(definition_db)
    assert manager.get_by_name("default") is None

    result = await create_workflows_registry(db=definition_db).call("reload_cache", {})

    assert result["success"] is True
    assert result["agents_synced"] > 0
    assert manager.get_by_name("default") is not None


def _setup(db: PostgresHubDatabase) -> AgentDefinitionManager:
    return AgentDefinitionManager(db)


def _insert_agent(
    mgr: AgentDefinitionManager,
    name: str = "test-agent",
    source: DefinitionSource = "installed",
    enabled: bool = True,
    tags: list[str] | None = None,
    **overrides: object,
) -> None:
    fields: dict[str, object] = {
        "prompts": {
            "persona": "Interactive guidance.",
            "agent": "Run the assigned task.",
        }
    }
    fields.update(overrides)
    body = make_agent_definition(name=name, enabled=enabled, **fields)
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

    def test_malformed_json_does_not_abort_list(self) -> None:
        class _Manager:
            def list_resolved(self, **_kwargs: object) -> list[SimpleNamespace]:
                return [
                    SimpleNamespace(
                        id="1",
                        name="good-a",
                        description="ok",
                        definition_json={"provider": "claude"},
                        enabled=True,
                        source="installed",
                        project_id=None,
                    ),
                    SimpleNamespace(
                        id="2",
                        name="bad",
                        description="broken",
                        definition_json="not-json{",
                        enabled=True,
                        source="installed",
                        project_id=None,
                    ),
                    SimpleNamespace(
                        id="3",
                        name="good-b",
                        description="ok",
                        definition_json={"provider": "codex"},
                        enabled=True,
                        source="installed",
                        project_id=None,
                    ),
                ]

        result = list_agent_definitions(cast(AgentDefinitionManager, _Manager()))

        names = [agent["name"] for agent in result["agents"]]
        assert result["success"] is True
        assert result["count"] == 3
        assert set(names) == {"good-a", "bad", "good-b"}

    def test_non_list_steps_do_not_abort_list(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "odd-steps")
        row = mgr.get_by_name("odd-steps")
        assert row is not None
        mgr.update(row.id, definition_json=json.dumps({"step_workflow": {"steps": 3}}))

        result = list_agent_definitions(mgr)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["agents"][0]["step_count"] == 0
        assert result["agents"][0]["has_steps"] is False


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
            surfaces=["spawn", "persona"],
            prompts={"persona": "Help test things calmly.", "agent": "read first"},
            timeout=300.0,
        )
        result = get_agent_definition(mgr, "detailed")
        agent = result["agent"]
        assert agent["prompts"]["persona"] == "Help test things calmly."
        assert agent["prompts"]["agent"] == "read first"
        assert agent["timeout"] == 300.0
        assert "max_turns" not in agent

    def test_detail_includes_prewarm_opt_out_and_tool_blocks(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        mgr = _setup(definition_db)
        _insert_agent(
            mgr,
            "reviewer",
            prewarm_pre_commit_store=False,
            blocked_mcp_tools=["gobby-tasks:close_task"],
        )
        agent = get_agent_definition(mgr, "reviewer")["agent"]
        assert agent["prewarm_pre_commit_store"] is False
        assert agent["blocked_mcp_tools"] == ["gobby-tasks:close_task"]

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
        result = create_agent_definition(
            mgr,
            "new-agent",
            {
                "provider": "claude",
                "prompts": {"agent": "Run the assigned task."},
                "workflows": {"rule_selectors": {"include": []}},
            },
        )
        assert result["success"] is True
        assert result["agent"]["name"] == "new-agent"

    def test_with_all_fields(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        result = create_agent_definition(
            mgr,
            "full-agent",
            {
                "description": "Full agent",
                "workflows": {"rule_selectors": {"include": []}},
                "prompts": {"agent": "Build things."},
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
            {
                "description": "Old payload",
                "workflows": {"rule_selectors": {"include": []}},
                "prompts": {"agent": "Run the assigned task."},
                "max_turns": 20,
            },
        )
        row = mgr.get_by_name("stale-limit-agent")

        assert result["success"] is True
        assert "max_turns" not in result["agent"]
        assert row is not None
        persisted = row.definition_json
        if isinstance(persisted, str):
            persisted = json.loads(persisted)
        assert "max_turns" not in persisted

    def test_duplicate_fails(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        definition = {
            "prompts": {"agent": "Run the assigned task."},
            "workflows": {"rule_selectors": {"include": []}},
        }
        create_agent_definition(mgr, "dup", definition.copy())
        result = create_agent_definition(mgr, "dup", definition.copy())
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_invalid_definition(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        result = create_agent_definition(mgr, "bad", {"checkout_mode": "invalid_isolation"})
        assert result["success"] is False
        assert "Validation failed" in result["error"]

    def test_persists_to_db(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        create_agent_definition(
            mgr,
            "persistent",
            {
                "description": "Stays in DB",
                "workflows": {"rule_selectors": {"include": []}},
                "prompts": {"agent": "Run the assigned task."},
            },
        )
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

    @pytest.mark.asyncio
    async def test_omit_step_workflow_preserves_stored_workflow(
        self, definition_db: PostgresHubDatabase
    ) -> None:
        mgr = _setup(definition_db)
        _insert_agent(mgr, "coder")
        update_agent_step_workflow(
            mgr,
            "coder",
            {
                "variables": {"goal": "ship"},
                "exit_condition": "done",
                "steps": [{"name": "implement"}],
            },
        )
        registry = create_workflows_registry(db=definition_db)

        omitted = await registry.call("update_agent_step_workflow", {"name": "coder"})
        stored = get_agent_definition(mgr, "coder")["agent"]["step_workflow"]

        assert omitted["success"] is True
        assert stored is not None
        assert stored["steps"][0]["name"] == "implement"

        cleared = await registry.call(
            "update_agent_step_workflow",
            {"name": "coder", "clear_step_workflow": True},
        )
        assert cleared["success"] is True
        assert get_agent_definition(mgr, "coder")["agent"]["step_workflow"] is None


class TestProjectScopedResolution:
    """Agent-definition tools resolve the caller's project row first, as spawn does."""

    _PROJECT = "6c1f7a52-3b0e-4d4a-9a55-2f0b8f1d9e01"

    def _seed(self, mgr: AgentDefinitionManager) -> None:
        _insert_agent(mgr, "reviewer", model="global-model")
        project_body = make_agent_definition(
            name="reviewer",
            model="project-model",
            prompts={"agent": "Run the assigned task."},
        )
        mgr.create(
            "reviewer",
            project_body.model_dump(mode="json"),
            project_id=self._PROJECT,
        )

    async def _call(
        self, db: PostgresHubDatabase, tool: str, args: dict[str, object]
    ) -> dict[str, Any]:
        registry = create_workflows_registry(db=db)
        token = set_project_context({"id": self._PROJECT})
        try:
            return cast(dict[str, Any], await registry.call(tool, args))
        finally:
            reset_project_context(token)

    async def test_get_resolves_project_row(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        self._seed(mgr)

        result = await self._call(definition_db, "get_agent_definition", {"name": "reviewer"})

        assert result["agent"]["model"] == "project-model"
        assert result["agent"]["project_id"] == self._PROJECT
        assert get_agent_definition(mgr, "reviewer")["agent"]["model"] == "global-model"

    async def test_list_shows_project_row_once(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        self._seed(mgr)

        result = await self._call(definition_db, "list_agent_definitions", {})

        reviewers = [a for a in result["agents"] if a["name"] == "reviewer"]
        assert [(a["model"], a["project_id"]) for a in reviewers] == [
            ("project-model", self._PROJECT)
        ]

    async def test_mutations_target_project_row(self, definition_db: PostgresHubDatabase) -> None:
        mgr = _setup(definition_db)
        self._seed(mgr)

        toggled = await self._call(
            definition_db, "toggle_agent_definition", {"name": "reviewer", "enabled": False}
        )
        ruled = await self._call(
            definition_db, "update_agent_rules", {"name": "reviewer", "add": ["extra-rule"]}
        )

        assert toggled["success"] is True
        assert ruled["success"] is True
        project_row = mgr.get_by_name("reviewer", project_id=self._PROJECT)
        global_row = mgr.get_by_name("reviewer")
        assert project_row is not None
        assert global_row is not None
        assert project_row.enabled is False
        assert project_row.definition_json["workflows"]["rules"] == ["extra-rule"]
        assert global_row.enabled is True
        assert "extra-rule" not in global_row.definition_json["workflows"].get("rules", [])
