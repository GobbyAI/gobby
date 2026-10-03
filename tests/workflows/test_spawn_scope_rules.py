"""Tests for the bundled rule that limits which agents a spawned agent may spawn."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.agent_resolver import resolve_agent
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

LIMIT_SPAWNABLE_AGENTS = "limit-spawnable-agents"


class _UnreachableSessions(SessionManager):
    """A session store whose lookups fail the way an unavailable hub does."""

    def resolve_session_reference(self, ref: str, project_id: str | None = None) -> str:
        raise RuntimeError("session store unavailable")


def _only_limit_rule_enabled(db: HubDatabase) -> RuleDefinitionManager:
    """Sync the bundled rules and leave only the rule under test enabled."""
    sync_bundled_rules(db, get_bundled_rules_path())
    manager = RuleDefinitionManager(db)
    for row in manager.list_all():
        if row.name != LIMIT_SPAWNABLE_AGENTS and row.enabled:
            manager.update(row.id, enabled=False)
    return manager


def _spawn_event(caller_id: str, tool_name: str, agent: str | None) -> HookEvent:
    """The before_tool event the MCP proxy builds for one spawn dispatch."""
    arguments: dict[str, Any] = {"prompt": "work"}
    if tool_name == "dispatch_batch":
        arguments = {"suggestions": [{"task_id": "#1"}]}
    if agent is not None:
        arguments["agent"] = agent
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-agents",
            "tool_name": tool_name,
            "arguments": arguments,
        },
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=caller_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": caller_id, "_mcp_proxy_dispatch": True},
    )


def _define_agent(db: HubDatabase, name: str, spawnable: list[str] | None) -> None:
    body: dict[str, Any] = {
        "name": name,
        "prompts": {"agent": "Work."},
        "workflows": {"rule_selectors": {"include": ["tag:default"]}},
    }
    if spawnable is not None:
        body["spawnable_agents"] = spawnable
    AgentDefinitionManager(db).create(name, body)


class TestLimitSpawnableAgents:
    @pytest.fixture
    def callers(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> dict[str, Session]:
        """A root session and spawned children running a listing and a listless definition."""
        project_id = str(sample_project["id"])
        _define_agent(temp_db, "spawn-scope-lister", ["spawn-scope-worker"])
        _define_agent(temp_db, "spawn-scope-listless", None)
        _define_agent(temp_db, "spawn-scope-anyone", ["*"])

        def register(name: str, parent: Session | None = None) -> Session:
            return session_manager.register(
                external_id=f"spawn-scope-{name}",
                machine_id=require_machine_id(),
                source="claude",
                project_id=project_id,
                parent_session_id=None if parent is None else parent.id,
                agent_depth=0 if parent is None else 1,
            )

        root = register("root")

        def spawned(agent_name: str) -> Session:
            child = register(agent_name, root)
            run = LocalAgentRunManager(temp_db).create(
                parent_session_id=root.id,
                provider="claude",
                prompt="work",
                agent_name=agent_name,
                child_session_id=child.id,
            )
            updated = session_manager.update_terminal_pickup_metadata(child.id, agent_run_id=run.id)
            assert updated is not None and updated.agent_run_id == run.id
            return updated

        return {
            "root": root,
            "lister": spawned("spawn-scope-lister"),
            "listless": spawned("spawn-scope-listless"),
            "anyone": spawned("spawn-scope-anyone"),
        }

    def test_rule_syncs_enabled_blocking_both_spawn_tools(self, temp_db: HubDatabase) -> None:
        sync_bundled_rules(temp_db, get_bundled_rules_path())

        row = RuleDefinitionManager(temp_db).get_by_name(LIMIT_SPAWNABLE_AGENTS)
        assert row is not None
        assert row.enabled is True
        effect = RuleDefinitionBody.model_validate(row.definition_json).resolved_effects[0]
        assert effect.type == "block"
        assert effect.mcp_tools == ["gobby-agents:spawn_agent", "gobby-agents:dispatch_batch"]
        assert effect.reason is not None
        assert "spawnable_agents" in effect.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("caller", "tool_name", "agent", "expected_decision"),
        [
            ("listless", "spawn_agent", "spawn-scope-worker", "block"),
            ("listless", "spawn_agent", None, "block"),
            ("lister", "spawn_agent", "spawn-scope-worker", "allow"),
            ("lister", "spawn_agent", "spawn-scope-other", "block"),
            ("lister", "spawn_agent", None, "block"),
            ("lister", "dispatch_batch", "spawn-scope-worker", "allow"),
            ("lister", "dispatch_batch", None, "block"),
            ("anyone", "spawn_agent", "spawn-scope-other", "allow"),
            ("anyone", "dispatch_batch", None, "allow"),
            ("root", "spawn_agent", "spawn-scope-other", "allow"),
            ("root", "dispatch_batch", None, "allow"),
        ],
    )
    async def test_spawned_callers_spawn_only_listed_agents(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
        caller: str,
        tool_name: str,
        agent: str | None,
        expected_decision: str,
    ) -> None:
        _only_limit_rule_enabled(temp_db)
        caller_id = callers[caller].id

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(caller_id, tool_name, agent), session_id=caller_id, variables={}
        )

        assert response.decision == expected_decision
        if expected_decision == "block":
            assert response.reason is not None
            assert LIMIT_SPAWNABLE_AGENTS in response.reason
            assert "spawnable_agents" in response.reason

    @pytest.mark.asyncio
    async def test_disabling_the_rule_lets_a_listless_agent_spawn(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
    ) -> None:
        manager = _only_limit_rule_enabled(temp_db)
        row = manager.get_by_name(LIMIT_SPAWNABLE_AGENTS)
        assert row is not None
        manager.update(row.id, enabled=False)
        caller_id = callers["listless"].id

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(caller_id, "spawn_agent", "spawn-scope-worker"),
            session_id=caller_id,
            variables={},
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_evaluation_error_refuses_the_spawn(
        self, temp_db: HubDatabase, callers: dict[str, Session]
    ) -> None:
        _only_limit_rule_enabled(temp_db)
        caller_id = callers["root"].id
        engine = RuleEngine(temp_db, session_manager=_UnreachableSessions(temp_db))

        response = await engine.evaluate(
            _spawn_event(caller_id, "spawn_agent", "spawn-scope-worker"),
            session_id=caller_id,
            variables={},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert LIMIT_SPAWNABLE_AGENTS in response.reason


def test_bundled_merge_orchestrator_may_spawn_merge_workers(temp_db: HubDatabase) -> None:
    sync_bundled_agents(temp_db)

    body = resolve_agent("merge-orchestrator", temp_db)

    assert body is not None
    assert body.spawnable_agents == ["merge-worker"]


def test_spawn_any_wildcard_stands_alone() -> None:
    with pytest.raises(ValueError, match="never both"):
        AgentDefinitionBody.model_validate(
            {
                "name": "spawn-scope-mixed",
                "prompts": {"agent": "Work."},
                "workflows": {},
                "spawnable_agents": ["*", "merge-worker"],
            }
        )
