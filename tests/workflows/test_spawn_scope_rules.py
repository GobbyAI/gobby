"""Tests for the bundled rules that limit what a spawned agent may spawn and with which network."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.sessions.clear_continuation import stage_clear_attempt, take_clear_handoff_marker
from gobby.sessions.handoff_records import build_handoff_payload
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
LIMIT_SPAWN_NETWORK_OVERRIDE = "limit-spawn-network-override"
SEAT_NO_SPAWN = "seat-no-spawn"


class _UnreachableSessions(SessionManager):
    """A session store whose lookups fail the way an unavailable hub does."""

    def resolve_session_reference(self, ref: str, project_id: str | None = None) -> str:
        raise RuntimeError("session store unavailable")


def _only_rules_enabled(db: HubDatabase, *names: str) -> RuleDefinitionManager:
    """Sync the bundled rules and leave only the rules under test enabled."""
    sync_bundled_rules(db, get_bundled_rules_path())
    manager = RuleDefinitionManager(db)
    for row in manager.list_all():
        if row.name not in names and row.enabled:
            manager.update(row.id, enabled=False)
    return manager


def _spawn_event(
    caller_id: str,
    tool_name: str,
    agent: str | None,
    suggestions: object = None,
    **extra: Any,
) -> HookEvent:
    """The before_tool event the MCP proxy builds for one spawn dispatch."""
    arguments: dict[str, Any] = {"prompt": "work"}
    if tool_name == "dispatch_batch":
        arguments = {"suggestions": [{"task_id": "#1"}] if suggestions is None else suggestions}
    if agent is not None:
        arguments["agent"] = agent
    arguments.update(extra)
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
        metadata={"_platform_session_id": caller_id},
    )


def _define_agent(
    db: HubDatabase, name: str, spawnable: list[str] | None, fallback: str | None = None
) -> None:
    body: dict[str, Any] = {
        "name": name,
        "prompts": {"agent": "Work."},
        "workflows": {"rule_selectors": {"include": ["tag:default"]}},
    }
    if spawnable is not None:
        body["spawnable_agents"] = spawnable
    if fallback is not None:
        body["fallback_agent"] = fallback
    AgentDefinitionManager(db).create(name, body)


def _register(
    session_manager: SessionManager, project_id: str, name: str, parent: Session | None = None
) -> Session:
    """A root session, or a depth-1 child of ``parent`` with no agent run yet."""
    return session_manager.register(
        external_id=f"spawn-scope-{name}",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
        parent_session_id=None if parent is None else parent.id,
        agent_depth=0 if parent is None else 1,
    )


def _spawned(
    db: HubDatabase,
    session_manager: SessionManager,
    root: Session,
    name: str,
    agent_name: str | None,
) -> Session:
    """A child of ``root`` bound to an agent run that names ``agent_name``."""
    child = _register(session_manager, root.project_id, name, root)
    run = LocalAgentRunManager(db).create(
        parent_session_id=root.id,
        provider="claude",
        prompt="work",
        agent_name=agent_name,
        child_session_id=child.id,
    )
    updated = session_manager.update_terminal_pickup_metadata(child.id, agent_run_id=run.id)
    assert updated is not None and updated.agent_run_id == run.id
    return updated


def _run_holder(session_manager: SessionManager, owner: Session) -> Session:
    """Another session at ``owner``'s depth that points at ``owner``'s agent run."""
    holder = session_manager.register(
        "spawn-scope-run-holder",
        owner.machine_id,
        owner.source,
        project_id=owner.project_id,
        agent_depth=owner.agent_depth,
    )
    updated = session_manager.update_terminal_pickup_metadata(
        holder.id, agent_run_id=owner.agent_run_id
    )
    assert updated is not None and updated.agent_run_id == owner.agent_run_id
    return updated


class TestLimitSpawnableAgents:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cleared", ["root", "coordinator", "caller"])
    @pytest.mark.parametrize("allowed", [False, True], ids=["forbidden-target", "allowed-target"])
    async def test_rule_spawn_scope_survives_clear(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
        cleared: str,
        allowed: bool,
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWNABLE_AGENTS)
        predecessor = callers["lister"] if cleared == "caller" else callers["root"]
        successor = session_manager.register(
            "clear-successor",
            predecessor.machine_id,
            predecessor.source,
            project_id=predecessor.project_id,
            agent_depth=predecessor.agent_depth,
        )
        session_manager.update_terminal_pickup_metadata(
            successor.id,
            agent_run_id=predecessor.agent_run_id,
        )
        attempt = uuid4().hex
        stage_clear_attempt(
            temp_db,
            predecessor.id,
            attempt_id=attempt,
            handoff=build_handoff_payload(current_state="Continue.", next_steps=["Continue."]),
            terminal_context=None,
            chat_context=None,
        )
        assert take_clear_handoff_marker(
            temp_db, predecessor.id, attempt_id=attempt, successor_id=successor.id
        )
        caller_id = callers["lister"].id if cleared == "coordinator" else successor.id
        target = "spawn-scope-worker" if allowed else "spawn-scope-other"
        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(caller_id, "spawn_agent", target),
            session_id=caller_id,
            variables={},
        )
        assert response.decision == ("allow" if allowed or cleared == "root" else "block")
        if response.decision == "block":
            assert response.reason is not None and "spawnable_agents" in response.reason

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
        _define_agent(temp_db, "spawn-scope-chained", None, fallback="spawn-scope-backup")
        _define_agent(temp_db, "spawn-scope-backup", None)
        _define_agent(temp_db, "spawn-scope-chain-lister", ["spawn-scope-chained"])
        _define_agent(
            temp_db, "spawn-scope-chain-both", ["spawn-scope-chained", "spawn-scope-backup"]
        )

        root = _register(session_manager, project_id, "root")

        def spawned(agent_name: str) -> Session:
            return _spawned(temp_db, session_manager, root, agent_name, agent_name)

        return {
            "root": root,
            "lister": spawned("spawn-scope-lister"),
            "listless": spawned("spawn-scope-listless"),
            "anyone": spawned("spawn-scope-anyone"),
            "chain-lister": spawned("spawn-scope-chain-lister"),
            "chain-both": spawned("spawn-scope-chain-both"),
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
        _only_rules_enabled(temp_db, LIMIT_SPAWNABLE_AGENTS)
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
    @pytest.mark.parametrize(
        ("caller", "tool_name", "agent", "suggestions", "expected_decision"),
        [
            # A suggestion's own agent overrides the top-level one.
            ("lister", "dispatch_batch", "spawn-scope-worker", [{"agent": "default"}], "block"),
            ("lister", "dispatch_batch", None, [{"agent": "spawn-scope-worker"}], "allow"),
            ("lister", "dispatch_batch", "spawn-scope-worker", [{"agent": "  "}], "allow"),
            (
                "lister",
                "dispatch_batch",
                None,
                [{"agent": "spawn-scope-worker"}, {"agent": "spawn-scope-other"}],
                "block",
            ),
            ("lister", "dispatch_batch", "spawn-scope-worker", "#1", "block"),
            ("lister", "dispatch_batch", "spawn-scope-worker", ["#1"], "block"),
            # Rules check requested names; admission checks the resolved project's chain.
            ("chain-lister", "spawn_agent", "spawn-scope-chained", None, "allow"),
            ("chain-both", "spawn_agent", "spawn-scope-chained", None, "allow"),
            (
                "chain-lister",
                "dispatch_batch",
                None,
                [{"agent": "spawn-scope-chained"}],
                "allow",
            ),
            ("chain-both", "dispatch_batch", "spawn-scope-chained", [{}], "allow"),
        ],
    )
    async def test_every_requested_spawn_target_must_be_listed(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
        caller: str,
        tool_name: str,
        agent: str | None,
        suggestions: object,
        expected_decision: str,
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWNABLE_AGENTS)
        caller_id = callers[caller].id

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(caller_id, tool_name, agent, suggestions),
            session_id=caller_id,
            variables={},
        )

        assert response.decision == expected_decision
        if expected_decision == "block":
            assert response.reason is not None
            assert LIMIT_SPAWNABLE_AGENTS in response.reason

    @pytest.mark.asyncio
    async def test_disabling_the_rule_lets_a_listless_agent_spawn(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
    ) -> None:
        manager = _only_rules_enabled(temp_db, LIMIT_SPAWNABLE_AGENTS)
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
        _only_rules_enabled(temp_db, LIMIT_SPAWNABLE_AGENTS)
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


class TestLimitSpawnNetworkOverride:
    @pytest.fixture
    def callers(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> dict[str, Session]:
        """A root session, spawned runs with and without authority, and broken identities."""
        root = _register(session_manager, str(sample_project["id"]), "root")
        return {
            "root": root,
            "default": _spawned(temp_db, session_manager, root, "default", "default"),
            "orchestrator": _spawned(
                temp_db, session_manager, root, "orchestrator", "orchestrator"
            ),
            "worker": _spawned(temp_db, session_manager, root, "worker", "spawn-scope-worker"),
            "nameless": _spawned(temp_db, session_manager, root, "nameless", None),
            "runless": _register(session_manager, root.project_id, "runless", root),
        }

    def test_rule_syncs_enabled_blocking_spawn_agent(self, temp_db: HubDatabase) -> None:
        sync_bundled_rules(temp_db, get_bundled_rules_path())

        row = RuleDefinitionManager(temp_db).get_by_name(LIMIT_SPAWN_NETWORK_OVERRIDE)
        assert row is not None
        assert row.enabled is True
        effect = RuleDefinitionBody.model_validate(row.definition_json).resolved_effects[0]
        assert effect.type == "block"
        assert effect.mcp_tools == ["gobby-agents:spawn_agent"]
        assert effect.reason is not None
        assert "network" in effect.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", ["root", "default", "orchestrator"])
    @pytest.mark.parametrize("network", ["none", "trusted"])
    async def test_root_default_and_orchestrator_choose_either_profile(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
        caller: str,
        network: str,
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWN_NETWORK_OVERRIDE)
        caller_id = callers[caller].id

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(caller_id, "spawn_agent", "spawn-scope-worker", network=network),
            session_id=caller_id,
            variables={},
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("network", ["none", "trusted"])
    async def test_other_spawned_caller_cannot_choose_a_profile(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
        network: str,
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWN_NETWORK_OVERRIDE)
        caller_id = callers["worker"].id

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(caller_id, "spawn_agent", "spawn-scope-worker", network=network),
            session_id=caller_id,
            variables={},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert LIMIT_SPAWN_NETWORK_OVERRIDE in response.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "override",
        [pytest.param({}, id="omitted"), pytest.param({"network": None}, id="explicit-null")],
    )
    async def test_other_spawned_caller_spawns_with_the_inherited_profile(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
        override: dict[str, Any],
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWN_NETWORK_OVERRIDE)
        caller_id = callers["worker"].id

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(caller_id, "spawn_agent", "spawn-scope-worker", **override),
            session_id=caller_id,
            variables={},
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "caller",
        [
            pytest.param("worker", id="forged-root-parent"),
            pytest.param("runless", id="run-less-child"),
            pytest.param("nameless", id="run-names-no-definition"),
        ],
    )
    async def test_unverified_identity_never_gains_authority(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
        caller: str,
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWN_NETWORK_OVERRIDE)
        caller_id = callers[caller].id
        event = _spawn_event(
            caller_id,
            "spawn_agent",
            "spawn-scope-worker",
            network="trusted",
            parent_session_id=callers["root"].id,
        )

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            event, session_id=caller_id, variables={}
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert LIMIT_SPAWN_NETWORK_OVERRIDE in response.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("owner", ["default", "orchestrator"])
    async def test_borrowed_run_never_gains_authority(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
        owner: str,
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWN_NETWORK_OVERRIDE)
        holder = _run_holder(session_manager, callers[owner])

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(holder.id, "spawn_agent", "spawn-scope-worker", network="trusted"),
            session_id=holder.id,
            variables={},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert LIMIT_SPAWN_NETWORK_OVERRIDE in response.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("owner", ["default", "orchestrator"])
    async def test_clear_successor_keeps_the_runs_authority(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
        owner: str,
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWN_NETWORK_OVERRIDE)
        predecessor = callers[owner]
        successor = _run_holder(session_manager, predecessor)
        attempt = uuid4().hex
        stage_clear_attempt(
            temp_db,
            predecessor.id,
            attempt_id=attempt,
            handoff=build_handoff_payload(current_state="Continue.", next_steps=["Continue."]),
            terminal_context=None,
            chat_context=None,
        )
        assert take_clear_handoff_marker(
            temp_db, predecessor.id, attempt_id=attempt, successor_id=successor.id
        )

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(successor.id, "spawn_agent", "spawn-scope-worker", network="trusted"),
            session_id=successor.id,
            variables={},
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_missing_caller_identity_refuses_an_override(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWN_NETWORK_OVERRIDE)
        root_id = callers["root"].id
        event = _spawn_event(
            root_id,
            "spawn_agent",
            "spawn-scope-worker",
            network="trusted",
            parent_session_id=root_id,
        )
        del event.metadata["_platform_session_id"]

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            event, session_id=root_id, variables={}
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert LIMIT_SPAWN_NETWORK_OVERRIDE in response.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("override", "expected_decision"),
        [
            pytest.param({"network": "trusted"}, "block", id="override-refused"),
            pytest.param({}, "allow", id="no-override-spared"),
        ],
    )
    async def test_evaluation_error_refuses_only_an_override(
        self,
        temp_db: HubDatabase,
        callers: dict[str, Session],
        override: dict[str, Any],
        expected_decision: str,
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWN_NETWORK_OVERRIDE)
        caller_id = callers["root"].id
        engine = RuleEngine(temp_db, session_manager=_UnreachableSessions(temp_db))

        response = await engine.evaluate(
            _spawn_event(caller_id, "spawn_agent", "spawn-scope-worker", **override),
            session_id=caller_id,
            variables={},
        )

        assert response.decision == expected_decision

    @pytest.mark.asyncio
    async def test_spawned_default_spawns_children_with_a_chosen_profile(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
    ) -> None:
        sync_bundled_agents(temp_db)
        _only_rules_enabled(temp_db, LIMIT_SPAWNABLE_AGENTS, LIMIT_SPAWN_NETWORK_OVERRIDE)
        caller_id = callers["default"].id

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(caller_id, "spawn_agent", "spawn-scope-worker", network="trusted"),
            session_id=caller_id,
            variables={},
        )

        body = resolve_agent("default", temp_db)
        assert body is not None
        assert body.spawnable_agents == ["*"]
        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_override_authority_leaves_seat_no_spawn_in_force(
        self,
        temp_db: HubDatabase,
        session_manager: SessionManager,
        callers: dict[str, Session],
    ) -> None:
        _only_rules_enabled(temp_db, LIMIT_SPAWN_NETWORK_OVERRIDE, SEAT_NO_SPAWN)
        caller_id = callers["orchestrator"].id

        response = await RuleEngine(temp_db, session_manager=session_manager).evaluate(
            _spawn_event(caller_id, "spawn_agent", "spawn-scope-worker", network="trusted"),
            session_id=caller_id,
            variables={"_agent_type": "orchestrator"},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert SEAT_NO_SPAWN in response.reason
        assert LIMIT_SPAWN_NETWORK_OVERRIDE not in response.reason


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
