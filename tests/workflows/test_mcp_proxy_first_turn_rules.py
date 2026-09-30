"""First-turn Gobby MCP proxy startup race against the initial-stop memory gate.

A pane-launched CLI can start its first turn before the Gobby stdio bridge
connects, so that turn's tool catalog has no Gobby tools. The bridge reports
readiness through ``POST /api/mcp/bridge/ready``, which sets
``_mcp_proxy_ready``; turn_start snapshots it into ``_mcp_proxy_ready_this_turn``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
MEMORY_DIRECTIVE = skill_fetch_directive("gobby:references/memory/overview.md")
RETRY_MARKER = "Gobby MCP proxy was not yet connected"


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    sync_bundled_rules(temp_db)
    return temp_db


def _event(event_type: HookEventType, **data: Any) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
    )


def _fresh_session_variables(**overrides: Any) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "_memory_initial_stop_checked": False,
        "loaded_skills": [],
        "open_tool_errors": [],
    }
    variables.update(overrides)
    return variables


async def _turn(engine: RuleEngine, variables: dict[str, Any]) -> tuple[HookResponse, HookResponse]:
    start = await engine.evaluate(
        _event(HookEventType.BEFORE_AGENT, prompt="report your session ref"),
        SESSION_ID,
        variables,
    )
    end = await engine.evaluate(_event(HookEventType.STOP), SESSION_ID, variables)
    return start, end


class TestInitialStopBeforeProxyConnects:
    @pytest.mark.asyncio
    async def test_first_turn_before_proxy_connects_is_not_blocked(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        variables = _fresh_session_variables()

        _, first_end = await _turn(engine, variables)

        assert MEMORY_DIRECTIVE not in (first_end.reason or "")
        assert first_end.decision == "allow", first_end.reason
        assert variables["_memory_initial_stop_checked"] is False

    @pytest.mark.asyncio
    async def test_gate_defers_to_first_turn_that_had_the_proxy(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        variables = _fresh_session_variables()

        await _turn(engine, variables)
        variables["_mcp_proxy_ready"] = True  # the bridge reported during turn one
        second_start, second_end = await _turn(engine, variables)

        assert RETRY_MARKER in (second_start.context or "")
        assert second_end.decision == "block"
        assert MEMORY_DIRECTIVE in (second_end.reason or "")
        assert variables["_memory_initial_stop_checked"] is True

    @pytest.mark.asyncio
    async def test_connected_first_turn_blocks_without_retry_guidance(
        self, db: HubDatabase
    ) -> None:
        engine = RuleEngine(db)
        variables = _fresh_session_variables(_mcp_proxy_ready=True)

        first_start, first_end = await _turn(engine, variables)

        assert RETRY_MARKER not in (first_start.context or "")
        assert first_end.decision == "block"
        assert MEMORY_DIRECTIVE in (first_end.reason or "")

    @pytest.mark.asyncio
    async def test_genuinely_absent_proxy_gets_no_retry_guidance(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        variables = _fresh_session_variables()

        turns = [await _turn(engine, variables) for _ in range(3)]

        for start, end in turns:
            assert RETRY_MARKER not in (start.context or "")
            assert MEMORY_DIRECTIVE not in (end.reason or "")
        assert variables["_memory_initial_stop_checked"] is False

    @pytest.mark.asyncio
    async def test_retry_guidance_is_delivered_once(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        variables = _fresh_session_variables()

        await _turn(engine, variables)
        variables["_mcp_proxy_ready"] = True
        second_start, _ = await _turn(engine, variables)
        third_start, _ = await _turn(engine, variables)

        assert RETRY_MARKER in (second_start.context or "")
        assert RETRY_MARKER not in (third_start.context or "")
