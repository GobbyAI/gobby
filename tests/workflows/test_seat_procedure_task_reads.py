"""A seat reads its task before its procedure and references load.

A runbook seat's procedure and listed references gate work in every context
epoch. Reading the assigned task (``get_task``) or the team's progress
(``team_progress``) is how a seat learns what that work is, so those reads
pass while the loads are still owed; every other tool call still waits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules
from tests.fixtures.agent_definitions import seed_seat

pytestmark = pytest.mark.unit

SESSION_ID = "22222222-2222-4222-8222-222222222222"
SEAT = "fixture-reading-seat"
PROCEDURE = "gobby:references/agents/messaging.md"
REFERENCE = "gobby:references/tasks/overview.md"


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    sync_bundled_rules(temp_db)
    seed_seat(temp_db, SEAT, include=["tag:runbook"])
    return RuleEngine(temp_db)


def _variables(*, loaded: list[str]) -> dict[str, Any]:
    return {
        "_agent_type": SEAT,
        "_seat_procedure": PROCEDURE,
        "_seat_references": [REFERENCE],
        "loaded_skills": [],
        "loaded_skill_references": loaded,
    }


def _mcp_call(server: str, tool: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "canonical_tool_kind": "mcp",
            "mcp_server": server,
            "mcp_tool": tool,
            "tool_input": {"server_name": server, "tool_name": tool, "arguments": {}},
        },
    )


async def _reason(engine: RuleEngine, tool: str, loaded: list[str]) -> str:
    response = await engine.evaluate(
        _mcp_call("gobby-tasks", tool),
        session_id=SESSION_ID,
        variables=_variables(loaded=loaded),
    )
    return response.reason or ""


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_task", "team_progress"])
@pytest.mark.parametrize("loaded", [[], [PROCEDURE]], ids=["nothing", "procedure"])
async def test_task_reads_pass_while_seat_loads_are_owed(
    engine: RuleEngine, tool: str, loaded: list[str]
) -> None:
    reason = await _reason(engine, tool, loaded)
    assert PROCEDURE not in reason
    assert REFERENCE not in reason


@pytest.mark.asyncio
async def test_other_work_still_waits_for_the_procedure(engine: RuleEngine) -> None:
    assert PROCEDURE in await _reason(engine, "update_task", [])


@pytest.mark.asyncio
async def test_other_work_still_waits_for_the_references(engine: RuleEngine) -> None:
    assert REFERENCE in await _reason(engine, "update_task", [PROCEDURE])
