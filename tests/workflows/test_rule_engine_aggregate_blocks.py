"""Tests for declarative rule block aggregation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.runtime_models import ConfigSnapshot
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.blocked_tool_recovery import format_aggregated_block_reason
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _runtime_with(values: dict[str, object]) -> Any:
    config = DaemonConfig()
    return cast(
        Any,
        SimpleNamespace(
            snapshot=ConfigSnapshot(
                revision=1,
                desired=config,
                active=config,
                row_revisions={},
                pending_restart_keys=frozenset(),
                failed_live_keys={},
                desired_values=values,
                active_values=values,
            )
        ),
    )


def _make_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Edit", "tool_input": {"file_path": "example.py"}},
    )


def _insert_rule(
    manager: RuleDefinitionManager,
    name: str,
    effects: list[RuleEffect],
    *,
    priority: int,
    when: str | None = None,
) -> None:
    body = RuleDefinitionBody(
        event=RuleTriggerEvent.BEFORE_TOOL,
        when=when,
        effects=effects,
    )
    manager.create(
        name=name,
        definition_json=json.dumps(body.model_dump(mode="json")),
        priority=priority,
        enabled=True,
    )


class TestAggregateBlocks:
    @pytest.mark.asyncio
    async def test_matching_blocks_aggregate_in_priority_order(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "second-gate",
            [RuleEffect(type="block", reason="Second gate")],
            priority=20,
        )
        _insert_rule(
            manager,
            "first-gate",
            [RuleEffect(type="block", reason="First gate")],
            priority=10,
        )

        response = await RuleEngine(db).evaluate(_make_event(), session_id=SESSION_ID, variables={})

        assert response.decision == "block"
        assert response.reason == (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "Multiple gates blocked while retrying Edit.\n"
            "1. [first-gate] First gate\n"
            "2. [second-gate] Second gate"
        )

    async def test_duplicate_skill_directives_are_rendered_once(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        python_directive = (
            "Load and fully read the skill in its own outer tool result: "
            'call_tool("gobby-skills", "get_skill", {"name":"python"}). Then continue.'
        )
        restraint_directive = (
            "Load and fully read the skill in its own outer tool result: "
            'call_tool("gobby-skills", "get_skill", {"name":"restraint"}). Then continue.'
        )
        _insert_rule(
            manager,
            "require-claimed-task-required-skills",
            [RuleEffect(type="block", reason=python_directive)],
            priority=10,
        )
        _insert_rule(
            manager,
            "require-python-skill",
            [RuleEffect(type="block", reason=python_directive)],
            priority=20,
        )
        _insert_rule(
            manager,
            "require-restraint-skill",
            [RuleEffect(type="block", reason=restraint_directive)],
            priority=30,
        )

        response = await RuleEngine(db).evaluate(_make_event(), session_id=SESSION_ID, variables={})

        assert response.reason == (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "Multiple gates blocked while retrying Edit.\n"
            f"1. [require-claimed-task-required-skills] {python_directive}\n"
            f"2. [require-restraint-skill] {restraint_directive}"
        )

    def test_aggregate_formatter_omits_placeholder_retry_target(self) -> None:
        assert format_aggregated_block_reason(
            [("first-gate", "First gate"), ("second-gate", "Second gate")],
            tool_name="-",
        ) == (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "1. [first-gate] First gate\n"
            "2. [second-gate] Second gate"
        )

    @pytest.mark.asyncio
    async def test_single_block_output_is_unchanged(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "solo-gate",
            [RuleEffect(type="block", reason="Only gate")],
            priority=10,
        )

        response = await RuleEngine(db).evaluate(_make_event(), session_id=SESSION_ID, variables={})

        assert response.reason == "Rule enforced by Gobby: [solo-gate]\nOnly gate"

    @pytest.mark.asyncio
    async def test_aggregate_blocks_false_restores_first_block_behavior(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "first-gate",
            [RuleEffect(type="block", reason="First gate")],
            priority=10,
        )
        _insert_rule(
            manager,
            "second-gate",
            [
                RuleEffect(type="set_variable", variable="second_ran", value=True),
                RuleEffect(type="block", reason="Second gate"),
            ],
            priority=20,
        )
        variables: dict[str, Any] = {}

        engine = RuleEngine(db, config_runtime=_runtime_with({"rules.aggregate_blocks": False}))
        response = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)

        assert response.reason == "Rule enforced by Gobby: [first-gate]\nFirst gate"
        assert "second_ran" not in variables

    @pytest.mark.asyncio
    async def test_first_blocker_side_effects_run_and_lookahead_side_effects_do_not(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "first-gate",
            [
                RuleEffect(type="set_variable", variable="first_ran", value=True),
                RuleEffect(type="block", reason="First gate"),
            ],
            priority=10,
        )
        _insert_rule(
            manager,
            "second-gate",
            [
                RuleEffect(type="set_variable", variable="second_ran", value=True),
                RuleEffect(type="mcp_call", server="gobby-memory", tool="create_memory"),
                RuleEffect(type="block", reason="Second gate"),
            ],
            priority=20,
        )
        variables: dict[str, Any] = {}

        response = await RuleEngine(db).evaluate(
            _make_event(), session_id=SESSION_ID, variables=variables
        )

        assert variables["first_ran"] is True
        assert "second_ran" not in variables
        assert "mcp_calls" not in response.metadata
        assert response.reason == (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "Multiple gates blocked while retrying Edit.\n"
            "1. [first-gate] First gate\n"
            "2. [second-gate] Second gate"
        )

    @pytest.mark.asyncio
    async def test_aggregate_acknowledges_first_and_lookahead_block_gates(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "mandatory-gate",
            [RuleEffect(type="block", reason="Load mandatory skill")],
            priority=10,
            when="not variables.get('mandatory_skill_loaded')",
        )
        _insert_rule(
            manager,
            "context7-gate",
            [
                RuleEffect(
                    type="block",
                    reason="Optional context7 nudge",
                    acknowledge_variable="nudge_fired",
                )
            ],
            priority=20,
            when="not variables.get('nudge_fired')",
        )
        variables: dict[str, Any] = {}
        engine = RuleEngine(db)

        first = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)
        variables["mandatory_skill_loaded"] = True
        second = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)

        assert first.reason == (
            "Rule enforced by Gobby: [aggregated:2-gates]\n"
            "Multiple gates blocked while retrying Edit.\n"
            "1. [mandatory-gate] Load mandatory skill\n"
            "2. [context7-gate] Optional context7 nudge"
        )
        assert variables["nudge_fired"] is True
        assert second.decision == "allow"

    @pytest.mark.asyncio
    async def test_single_acknowledged_block_only_fires_once(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "context7-gate",
            [
                RuleEffect(
                    type="block",
                    reason="Optional context7 nudge",
                    acknowledge_variable="nudge_fired",
                )
            ],
            priority=10,
            when="not variables.get('nudge_fired')",
        )
        variables: dict[str, Any] = {}
        engine = RuleEngine(db)

        first = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)
        second = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)

        assert first.reason == "Rule enforced by Gobby: [context7-gate]\nOptional context7 nudge"
        assert variables["nudge_fired"] is True
        assert second.decision == "allow"

    @pytest.mark.asyncio
    async def test_repeated_identical_aggregate_uses_verbose_once(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "first-gate",
            [RuleEffect(type="block", reason="First gate")],
            priority=10,
        )
        _insert_rule(
            manager,
            "second-gate",
            [RuleEffect(type="block", reason="Second gate")],
            priority=20,
        )
        variables: dict[str, Any] = {}
        engine = RuleEngine(db)

        first = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)
        second = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)

        assert "1. [first-gate] First gate" in (first.reason or "")
        assert second.reason is not None
        assert second.reason.startswith("Rule enforced by Gobby: [aggregated:2-gates]")
        assert "full reason shown earlier" in second.reason

    @pytest.mark.asyncio
    async def test_reduced_remaining_gate_set_renders_fully(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _insert_rule(
            manager,
            "first-gate",
            [RuleEffect(type="block", reason="First gate")],
            priority=10,
            when="not variables.get('skip_first', False)",
        )
        _insert_rule(
            manager,
            "second-gate",
            [RuleEffect(type="block", reason="Second gate")],
            priority=20,
        )
        variables: dict[str, Any] = {}
        engine = RuleEngine(db)

        first = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)
        variables["skip_first"] = True
        second = await engine.evaluate(_make_event(), session_id=SESSION_ID, variables=variables)

        assert "aggregated:2-gates" in (first.reason or "")
        assert second.reason == "Rule enforced by Gobby: [second-gate]\nSecond gate"
