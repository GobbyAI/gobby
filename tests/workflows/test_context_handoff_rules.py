"""Tests for context-handoff rules.

Covers the autonomous ``clear_session`` block on ``gobby-sessions:set_handoff``
and the pre-compact state boundary carried by ``preserve-context-on-compact``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
BLOCK_REASON = "Autonomous sessions hand off in place."
PLAN_MODE_BLOCK_REASON = "Plan Mode handoffs compact in place."


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return temp_db


def _set_handoff_event(
    arguments: Any,
    *,
    tool_name: str = "mcp__gobby__call_tool",
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    if tool_name in {"call_tool", "mcp__gobby__call_tool"}:
        tool_input: Any = {
            "server_name": "gobby-sessions",
            "tool_name": "set_handoff",
            "arguments": arguments,
        }
    else:
        tool_input = arguments
    data: dict[str, Any] = {"tool_name": tool_name, "tool_input": tool_input}
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={},
    )


async def _evaluate(db: HubDatabase, event: HookEvent, variables: dict[str, Any]) -> HookResponse:
    return await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables=variables)


def _blocked_by_rule(response: HookResponse) -> bool:
    return response.decision == "block" and BLOCK_REASON in (response.reason or "")


HANDOFF_ARGUMENTS = {"current_state": "midway", "next_steps": ["finish"]}


class TestBlockAutonomousClearSession:
    def test_rule_is_bundled_with_autonomous_audience(self, db: HubDatabase) -> None:
        row = RuleDefinitionManager(db).get_by_name("block-autonomous-clear-session")
        assert row is not None
        assert row.enabled is True
        assert row.priority == 12
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.audience == "autonomous"
        effects = body.resolved_effects
        assert effects[0].type == "block"
        assert effects[0].mcp_tools == ["gobby-sessions:set_handoff"]

    @pytest.mark.parametrize(
        "variables",
        [{"is_spawned_agent": True}, {"_agent_type": "autonomous"}],
        ids=["spawned-agent", "autonomous-agent-type"],
    )
    @pytest.mark.parametrize(
        "source",
        [SessionSource.CLAUDE, SessionSource.CODEX, SessionSource.GROK],
    )
    @pytest.mark.asyncio
    async def test_blocks_clear_session_for_autonomous_sessions(
        self, db: HubDatabase, variables: dict[str, Any], source: SessionSource
    ) -> None:
        event = _set_handoff_event({**HANDOFF_ARGUMENTS, "clear_session": True}, source=source)

        response = await _evaluate(db, event, dict(variables))

        assert _blocked_by_rule(response)
        assert "without clear_session" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_blocks_clear_session_on_the_direct_mcp_tool(self, db: HubDatabase) -> None:
        event = _set_handoff_event(
            {**HANDOFF_ARGUMENTS, "clear_session": True},
            tool_name="mcp__gobby-sessions__set_handoff",
        )

        response = await _evaluate(db, event, {"is_spawned_agent": True})

        assert _blocked_by_rule(response)

    @pytest.mark.asyncio
    async def test_blocks_clear_session_from_json_string_arguments(self, db: HubDatabase) -> None:
        event = _set_handoff_event(json.dumps({**HANDOFF_ARGUMENTS, "clear_session": True}))

        response = await _evaluate(db, event, {"is_spawned_agent": True})

        assert _blocked_by_rule(response)

    @pytest.mark.asyncio
    async def test_allows_clear_session_for_interactive_sessions(self, db: HubDatabase) -> None:
        event = _set_handoff_event({**HANDOFF_ARGUMENTS, "clear_session": True})

        response = await _evaluate(db, event, {"is_spawned_agent": False})

        assert not _blocked_by_rule(response)

    @pytest.mark.parametrize(
        "arguments",
        [HANDOFF_ARGUMENTS, {**HANDOFF_ARGUMENTS, "clear_session": False}],
        ids=["absent", "false"],
    )
    @pytest.mark.asyncio
    async def test_allows_in_place_handoff_for_autonomous_sessions(
        self, db: HubDatabase, arguments: dict[str, Any]
    ) -> None:
        event = _set_handoff_event(arguments)

        response = await _evaluate(db, event, {"is_spawned_agent": True})

        assert not _blocked_by_rule(response)


class TestBlockPlanModeClearSession:
    def test_rule_is_installed_enabled_and_uses_resolved_plan_mode(self, db: HubDatabase) -> None:
        row = RuleDefinitionManager(db).get_by_name("block-clear-session-in-plan-mode")
        assert row is not None
        assert row.source == "installed"
        assert row.enabled is True
        assert row.priority == 13
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert "variables.get('plan_mode')" in (body.when or "")
        effects = body.resolved_effects
        assert effects[0].type == "block"
        assert effects[0].mcp_tools == ["gobby-sessions:set_handoff"]

    @pytest.mark.parametrize(
        "rule_name",
        [
            "nudge-compact-on-context-pressure",
            "nudge-compact-on-context-pressure-mid-turn",
        ],
    )
    def test_installed_context_warning_explains_task_boundaries(
        self, db: HubDatabase, rule_name: str
    ) -> None:
        row = RuleDefinitionManager(db).get_by_name(rule_name)
        assert row is not None
        assert row.source == "installed"
        body = RuleDefinitionBody.model_validate(row.definition_json)
        template = body.resolved_effects[0].template or ""
        assert "Planning, review, and ongoing task work use this compact path" in template
        assert "only between tasks after the current task closes" in template

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            pytest.param(
                "mcp__gobby__call_tool",
                {**HANDOFF_ARGUMENTS, "clear_session": True},
                id="normalized-wrapper",
            ),
            pytest.param(
                "call_tool",
                {**HANDOFF_ARGUMENTS, "clear_session": True},
                id="plain-wrapper",
            ),
            pytest.param(
                "mcp__gobby-sessions__set_handoff",
                {**HANDOFF_ARGUMENTS, "clear_session": True},
                id="direct-tool",
            ),
            pytest.param(
                "mcp__gobby__call_tool",
                json.dumps({**HANDOFF_ARGUMENTS, "clear_session": True}),
                id="json-arguments",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_blocks_clear_session_during_plan_mode(
        self, db: HubDatabase, tool_name: str, arguments: Any
    ) -> None:
        event = _set_handoff_event(arguments, tool_name=tool_name)

        response = await _evaluate(
            db,
            event,
            {"plan_mode": True, "is_spawned_agent": False},
        )

        assert response.decision == "block"
        assert PLAN_MODE_BLOCK_REASON in (response.reason or "")

    @pytest.mark.parametrize(
        "arguments",
        [HANDOFF_ARGUMENTS, {**HANDOFF_ARGUMENTS, "clear_session": False}],
        ids=["absent", "false"],
    )
    @pytest.mark.asyncio
    async def test_allows_in_place_handoff_during_plan_mode(
        self, db: HubDatabase, arguments: dict[str, Any]
    ) -> None:
        event = _set_handoff_event(arguments)

        response = await _evaluate(
            db,
            event,
            {"plan_mode": True, "is_spawned_agent": False},
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_allows_clear_session_for_non_plan_interactive_session(
        self, db: HubDatabase
    ) -> None:
        event = _set_handoff_event({**HANDOFF_ARGUMENTS, "clear_session": True})

        response = await _evaluate(
            db,
            event,
            {"plan_mode": False, "is_spawned_agent": False},
        )

        assert response.decision == "allow"


class TestPreserveContextOnCompact:
    def test_defers_epoch_marker_reset_until_successor_turn_start(self, db: HubDatabase) -> None:
        row = RuleDefinitionManager(db).get_by_name("preserve-context-on-compact")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "pre_compact"

        assignments = {
            effect.variable: effect.value
            for effect in body.resolved_effects
            if effect.type == "set_variable"
        }

        assert assignments["context_compact_mid_turn_pressure_band"] == "none"
        assert assignments["context_compact_tool_calls_since_nudge"] == 0
        assert assignments["context_compact_block_message"] == ""
        assert "context_compact_handoff_result" not in assignments
        assert "context_compact_highest_announced_threshold" not in assignments
