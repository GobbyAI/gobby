"""Integration tests for mandatory memory recall retrieval rules."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "287cefb3-355b-4795-a64d-2f52bc4be2a8"
EXTERNAL_SESSION_ID = "f9d7010b-681c-41cd-8b99-6c778f832831"
RULE_NAMES = {
    "guard-plan-memory-writes",
    "require-memory-recall-before-tool",
    "require-memory-recall-before-turn-end",
}


def _delivery(
    recall_request_id: str,
    *,
    origin_turn_seq: int = 1,
    status: str = "pending",
) -> dict[str, object]:
    return {
        "recall_request_id": recall_request_id,
        "origin_turn_seq": origin_turn_seq,
        "project_id": "project-1",
        "status": status,
        "references": [{"memory_id": f"memory-{origin_turn_seq}", "rank": 1}],
    }


def _event(
    event_type: HookEventType,
    *,
    tool_name: str = "",
    tool_input: dict[str, object] | None = None,
) -> HookEvent:
    data: dict[str, object] = {"tool_name": tool_name}
    if tool_input is not None:
        data["tool_input"] = tool_input
    return HookEvent(
        event_type=event_type,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": SESSION_ID},
    )


def _memory_tool_event(tool_name: str) -> HookEvent:
    return _event(
        HookEventType.BEFORE_TOOL,
        tool_name="mcp__gobby__call_tool",
        tool_input={
            "server_name": "gobby-memory",
            "tool_name": tool_name,
            "arguments": {},
        },
    )


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    with temp_db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET enabled = FALSE")
        for rule_name in RULE_NAMES:
            conn.execute(
                "UPDATE rule_definitions SET enabled = TRUE WHERE name = %s",
                (rule_name,),
            )
    return RuleEngine(temp_db)


@pytest.mark.asyncio
async def test_exact_oldest_retrieval_call_is_allowed(engine: RuleEngine) -> None:
    variables = {"memory_recall_deliveries": [_delivery("request-first")]}
    event = _event(
        HookEventType.BEFORE_TOOL,
        tool_name="mcp__gobby__call_tool",
        tool_input={
            "server_name": "gobby-memory",
            "tool_name": "get_recall_memories",
            "arguments": {"recall_request_id": "request-first"},
        },
    )

    result = await engine.evaluate(event, SESSION_ID, variables)

    assert result.decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_tool", ["create_memory", "update_memory"])
@pytest.mark.parametrize(
    "planning_context",
    [
        pytest.param({"plan_mode": True}, id="interactive-plan-mode"),
        pytest.param({"_agent_type": "planner"}, id="planner"),
        pytest.param({"_agent_type": "plan-adversary"}, id="plan-adversary"),
        pytest.param(
            {"_agent_type": "plan-adversary-taskless"},
            id="plan-adversary-taskless",
        ),
        pytest.param({"_agent_type": "plan-enhancer"}, id="plan-enhancer"),
        pytest.param(
            {"_agent_type": "plan-enhancer-taskless"},
            id="plan-enhancer-taskless",
        ),
    ],
)
async def test_plan_memory_write_blocks_once_then_allows_retry(
    engine: RuleEngine,
    memory_tool: str,
    planning_context: dict[str, object],
) -> None:
    variables = dict(planning_context)
    event = _memory_tool_event(memory_tool)

    first = await engine.evaluate(event, SESSION_ID, variables)
    second = await engine.evaluate(event, SESSION_ID, variables)

    assert first.decision == "block"
    assert first.reason is not None
    assert "Use the plan artifact or evidence for plan drafts" in first.reason
    assert variables["plan_memory_write_nudge_fired"] is True
    assert second.decision == "allow"


@pytest.mark.asyncio
async def test_plan_memory_guard_does_not_affect_normal_sessions(
    engine: RuleEngine,
) -> None:
    variables: dict[str, object] = {}

    result = await engine.evaluate(
        _memory_tool_event("create_memory"),
        SESSION_ID,
        variables,
    )

    assert result.decision == "allow"
    assert "plan_memory_write_nudge_fired" not in variables


@pytest.mark.asyncio
async def test_plan_memory_guard_does_not_affect_non_write_memory_tools(
    engine: RuleEngine,
) -> None:
    variables: dict[str, object] = {"plan_mode": True}

    result = await engine.evaluate(
        _memory_tool_event("search_memories"),
        SESSION_ID,
        variables,
    )

    assert result.decision == "allow"
    assert "plan_memory_write_nudge_fired" not in variables


@pytest.mark.asyncio
async def test_pending_recall_blocks_without_acknowledging_plan_memory_guard(
    engine: RuleEngine,
) -> None:
    variables: dict[str, object] = {
        "plan_mode": True,
        "memory_recall_deliveries": [_delivery("request-first")],
    }

    result = await engine.evaluate(
        _memory_tool_event("create_memory"),
        SESSION_ID,
        variables,
    )

    assert result.decision == "block"
    assert result.reason is not None
    assert "get_recall_memories" in result.reason
    assert variables.get("plan_memory_write_nudge_fired") is not True


@pytest.mark.parametrize(
    "tool_input",
    [
        {
            "server_name": "gobby-memory",
            "tool_name": "get_memory",
            "arguments": {"recall_request_id": "request-first"},
        },
        {
            "server_name": "gobby-memory",
            "tool_name": "get_recall_memories",
            "arguments": {"recall_request_id": "request-wrong"},
        },
        {"cmd": "git status --short"},
    ],
)
@pytest.mark.asyncio
async def test_other_tools_and_wrong_request_ids_are_blocked(
    engine: RuleEngine,
    tool_input: dict[str, object],
) -> None:
    variables = {"memory_recall_deliveries": [_delivery("request-first")]}
    event = _event(
        HookEventType.BEFORE_TOOL,
        tool_name="mcp__gobby__call_tool",
        tool_input=tool_input,
    )

    result = await engine.evaluate(event, SESSION_ID, variables)

    assert result.decision == "block"
    assert result.reason is not None
    assert "get_recall_memories" in result.reason
    assert "request-first" in result.reason
    assert "This is the only permitted call" in result.reason
    assert "Issue it alone, not inside a parallel batch." in result.reason


@pytest.mark.asyncio
async def test_second_pending_request_is_blocked_until_oldest_is_retrieved(
    engine: RuleEngine,
) -> None:
    variables = {
        "memory_recall_deliveries": [
            _delivery("request-first", origin_turn_seq=1),
            _delivery("request-second", origin_turn_seq=2),
        ]
    }
    event = _event(
        HookEventType.BEFORE_TOOL,
        tool_name="mcp__gobby__call_tool",
        tool_input={
            "server_name": "gobby-memory",
            "tool_name": "get_recall_memories",
            "arguments": {"recall_request_id": "request-second"},
        },
    )

    result = await engine.evaluate(event, SESSION_ID, variables)

    assert result.decision == "block"
    assert result.reason is not None
    assert "request-first" in result.reason
    assert "request-second" not in result.reason


@pytest.mark.asyncio
async def test_turn_end_is_blocked_while_recall_is_pending(engine: RuleEngine) -> None:
    variables = {"memory_recall_deliveries": [_delivery("request-stop")]}

    result = await engine.evaluate(
        _event(HookEventType.STOP),
        SESSION_ID,
        variables,
    )

    assert result.decision == "block"
    assert result.reason is not None
    assert "request-stop" in result.reason
    assert "This is the only permitted call" in result.reason
    assert "Issue it alone, not inside a parallel batch." in result.reason


@pytest.mark.asyncio
async def test_completed_and_invalid_deliveries_do_not_gate_tools(engine: RuleEngine) -> None:
    variables = {
        "memory_recall_deliveries": [
            _delivery("request-complete", status="complete"),
            {
                "recall_request_id": "request-empty",
                "origin_turn_seq": 2,
                "status": "pending",
                "references": [],
            },
        ]
    }

    result = await engine.evaluate(
        _event(
            HookEventType.BEFORE_TOOL,
            tool_name="Bash",
            tool_input={"cmd": "git status --short"},
        ),
        SESSION_ID,
        variables,
    )

    assert result.decision == "allow"
