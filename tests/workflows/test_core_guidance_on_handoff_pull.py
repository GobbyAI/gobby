"""After a compaction, one instruction bundle follows the handoff pull.

The first post-compact turn owes ``get_handoff`` and starts a fresh context
epoch with an empty skill ledger. The bundled rules must ask for every owed
instruction in one ``get_skill(names=[...])`` call delivered with the pull's
result, so the context costs the pull plus one load, and no skill or
reference gate refuses work once that load completes.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
SEAT_PROCEDURE = "gobby:references/agents/messaging.md"
SEAT_REFERENCE = "gobby:references/tasks/overview.md"
OWED = [
    "gobby:references/memory/overview.md",
    "brevity",
    "restraint",
    "gobby:references/development/obligations.md",
    "gobby:references/code-index/overview.md",
    "gobby:references/plan/overview.md",
    SEAT_PROCEDURE,
    SEAT_REFERENCE,
]
_BATCH_CALL = re.compile(r'call_tool\("gobby-skills", "get_skill", (\{"names":\[[^\]]*\]\})\)')


def _post_compact_variables(*, pull_pending: bool) -> dict[str, Any]:
    return {
        "handoff_pull_pending": pull_pending,
        "loaded_skills": [],
        "loaded_skill_references": [],
        "claimed_task_is_source_work": True,
        "code_index_available": True,
        "plan_mode": True,
        "plan_skill_directive_delivered": True,
        "_seat_procedure": SEAT_PROCEDURE,
        "_seat_references": [SEAT_REFERENCE],
    }


def _event(event_type: HookEventType, data: dict[str, Any] | None = None) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def _mcp_event(event_type: HookEventType, server: str, tool: str, **extra: Any) -> HookEvent:
    return _event(
        event_type,
        {
            "tool_name": "mcp__gobby__call_tool",
            "canonical_tool_kind": "mcp",
            "mcp_server": server,
            "mcp_tool": tool,
            "tool_input": {"server_name": server, "tool_name": tool, "arguments": {}},
            **extra,
        },
    )


def _load_calls(text: str | None) -> list[list[str]]:
    return [json.loads(match)["names"] for match in _BATCH_CALL.findall(text or "")]


def _loaded(variables: dict[str, Any], names: list[str]) -> dict[str, Any]:
    return {
        **variables,
        "handoff_pull_pending": False,
        "loaded_skills": [name for name in names if ":" not in name],
        "loaded_skill_references": [name for name in names if ":" in name],
    }


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    sync_bundled_rules(temp_db)
    return RuleEngine(temp_db)


@pytest.mark.asyncio
async def test_post_compact_costs_the_pull_plus_one_load(engine: RuleEngine) -> None:
    pending = _post_compact_variables(pull_pending=True)
    first_turn = await engine.evaluate(
        _event(HookEventType.BEFORE_AGENT, {"prompt": "continue"}),
        session_id=SESSION_ID,
        variables=pending,
    )
    assert '"get_skill"' not in (first_turn.context or "")

    pulled = await engine.evaluate(
        _mcp_event(
            HookEventType.AFTER_TOOL,
            "gobby-sessions",
            "get_handoff",
            tool_output={"success": True, "result": {"found": True, "handoff": "state"}},
        ),
        session_id=SESSION_ID,
        variables={**pending, "handoff_pull_pending": False},
    )
    assert _load_calls(pulled.context) == [OWED]
    assert (pulled.context or "").count("call_tool(") == 1

    loaded = _loaded(pending, OWED)
    gated = [
        _event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Write",
                "canonical_tool_kind": "write",
                "canonical_file_path": "/repo/.gobby/plans/draft.md",
                "tool_input": {"file_path": "/repo/.gobby/plans/draft.md", "content": "x"},
            },
        ),
        _event(
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "Grep",
                "canonical_tool_kind": "search",
                "tool_input": {"pattern": "needle", "path": "src"},
            },
        ),
        _mcp_event(HookEventType.BEFORE_TOOL, "gobby-tasks", "get_task"),
        _event(HookEventType.BEFORE_AGENT, {"prompt": "next"}),
    ]
    for event in gated:
        response = await engine.evaluate(event, session_id=SESSION_ID, variables=loaded)
        assert "get_skill" not in (response.reason or ""), event.data
        assert "get_skill" not in (response.context or ""), event.data


@pytest.mark.asyncio
async def test_cold_start_names_every_owed_instruction_in_one_call(engine: RuleEngine) -> None:
    response = await engine.evaluate(
        _event(HookEventType.BEFORE_AGENT, {"prompt": "start"}),
        session_id=SESSION_ID,
        variables=_post_compact_variables(pull_pending=False),
    )

    assert _load_calls(response.context) == [OWED]


@pytest.mark.asyncio
async def test_unavailable_instruction_is_not_requested_again(engine: RuleEngine) -> None:
    variables = {
        **_post_compact_variables(pull_pending=False),
        "unavailable_instructions": [SEAT_REFERENCE],
    }

    response = await engine.evaluate(
        _event(HookEventType.BEFORE_AGENT, {"prompt": "start"}),
        session_id=SESSION_ID,
        variables=variables,
    )

    assert _load_calls(response.context) == [[name for name in OWED if name != SEAT_REFERENCE]]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_skill", "get_skill_file", "list_skills", "search_skills"])
async def test_bootstrap_tools_pass_every_gate_before_any_load(
    engine: RuleEngine, tool: str
) -> None:
    """No later skill or reference gate refuses the calls that satisfy it (#25580 item 9)."""
    variables = _post_compact_variables(pull_pending=False)
    event = _mcp_event(HookEventType.BEFORE_TOOL, "gobby-skills", tool)
    event.data["tool_input"]["arguments"] = {"names": OWED}

    response = await engine.evaluate(event, session_id=SESSION_ID, variables=variables)

    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
async def test_the_pull_itself_passes_while_it_is_owed(engine: RuleEngine) -> None:
    response = await engine.evaluate(
        _mcp_event(HookEventType.BEFORE_TOOL, "gobby-sessions", "get_handoff"),
        session_id=SESSION_ID,
        variables=_post_compact_variables(pull_pending=True),
    )

    assert response.decision == "allow", response.reason


@pytest.mark.asyncio
async def test_skill_loads_still_wait_for_the_pull(engine: RuleEngine) -> None:
    response = await engine.evaluate(
        _mcp_event(HookEventType.BEFORE_TOOL, "gobby-skills", "get_skill"),
        session_id=SESSION_ID,
        variables=_post_compact_variables(pull_pending=True),
    )

    assert response.decision == "block"
    assert "get_handoff" in (response.reason or "")
