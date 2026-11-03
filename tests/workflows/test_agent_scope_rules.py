"""Tests for agent-scoped rules and agent-specific rule YAML files.

Covers:
- RuleEngine agent_scope filtering (_agent_type variable)
- Developer rules (no-coderabbit, require-tests-pass) scoped to developer
- QA rules (no-code-writing) scoped to qa
- Coordinator rules (no-code-writing) scoped to coordinator
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from tests.fixtures.agent_definitions import seed_seat

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    # Named seats must resolve their definitions; each selects every rule.
    for seat in ("fixture-builder", "fixture-checker", "fixture-router"):
        seed_seat(temp_db, seat, include=["*"])
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _make_event(
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def _insert_rule(
    manager: RuleDefinitionManager,
    name: str,
    body: RuleDefinitionBody,
    priority: int = 100,
) -> str:
    body.mandatory = True
    row = manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        priority=priority,
        enabled=True,
    )
    return row.id


# ═══════════════════════════════════════════════════════════════════════
# RuleEngine agent_scope filtering
# ═══════════════════════════════════════════════════════════════════════


class TestAgentScopeFiltering:
    """RuleEngine filters rules by agent_scope based on _agent_type variable."""

    @pytest.mark.asyncio
    async def test_global_rule_fires_without_agent_type(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """Rules with no agent_scope fire for any session."""
        _insert_rule(
            manager,
            "global-block",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", tools=["Write"], reason="Global block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_global_rule_fires_with_agent_type(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """Rules with no agent_scope fire even when _agent_type is set."""
        _insert_rule(
            manager,
            "global-block",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
                effects=[RuleEffect(type="block", tools=["Write"], reason="Global block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {"_agent_type": "fixture-builder"}
        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.decision == "block"

    @pytest.mark.asyncio
    async def test_scoped_rule_fires_for_matching_agent(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """Rules with agent_scope fire when _agent_type matches."""
        _insert_rule(
            manager,
            "dev-only-block",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
                agent_scope=["fixture-builder"],
                effects=[RuleEffect(type="block", tools=["Write"], reason="Dev block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {"_agent_type": "fixture-builder"}
        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.decision == "block"
        assert "Dev block" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_scoped_rule_skipped_for_non_matching_agent(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """Rules with agent_scope are skipped when _agent_type doesn't match."""
        _insert_rule(
            manager,
            "dev-only-block",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
                agent_scope=["fixture-builder"],
                effects=[RuleEffect(type="block", tools=["Write"], reason="Dev block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {"_agent_type": "fixture-checker"}
        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_scoped_rule_skipped_without_agent_type(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """Rules with agent_scope are skipped when no _agent_type is set."""
        _insert_rule(
            manager,
            "dev-only-block",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
                agent_scope=["fixture-builder"],
                effects=[RuleEffect(type="block", tools=["Write"], reason="Dev block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_multi_scope_rule_matches_any(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """Rules with multiple agent_scope entries match any of them."""
        _insert_rule(
            manager,
            "dev-qa-block",
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
                agent_scope=["fixture-builder", "fixture-checker"],
                effects=[RuleEffect(type="block", tools=["Write"], reason="Shared block")],
            ),
            priority=10,
        )

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})

        # Should match for developer
        variables: dict[str, Any] = {"_agent_type": "fixture-builder"}
        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "block"

        # Should match for qa
        variables = {"_agent_type": "fixture-checker"}
        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "block"

        # Should NOT match for coordinator
        variables = {"_agent_type": "fixture-router"}
        response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)
        assert response.decision == "allow"


# ═══════════════════════════════════════════════════════════════════════
# Developer agent rules
# ═══════════════════════════════════════════════════════════════════════
