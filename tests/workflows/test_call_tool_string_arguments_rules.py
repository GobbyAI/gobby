"""Rule conditions see the call_tool target arguments that dispatch runs (#23125).

A nested wrapper route can carry its target arguments as a JSON string. Rule
conditions must read the same parsed dict the proxy dispatches, and input the
proxy refuses must reach conditions as a typed refusal rather than a raw string.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.templating import TemplatingMixin
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
CLAIMED_TASKS_REASON = "Use clear_session=false while working inside a task."
CLAIMED = {"claimed_tasks": {"fe063aea-a379-4cb8-b55e-1519f14bbfea": "#23125"}}
ROUTE = {"server_name": "gobby-sessions", "tool_name": "set_handoff"}
HANDOFF = {"current_state": "midway", "next_steps": ["finish"]}


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    sync_bundled_rules(temp_db, get_bundled_rules_path())
    temp_db.execute("UPDATE rule_definitions SET source = 'installed' WHERE source = 'template'")
    return temp_db


def _call_tool_event(tool_input: dict[str, Any]) -> HookEvent:
    data: dict[str, Any] = {"tool_name": "mcp__gobby__call_tool", "tool_input": tool_input}
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={},
    )


async def _evaluate(db: HubDatabase, event: HookEvent) -> HookResponse:
    return await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables=dict(CLAIMED))


def _condition_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR and "Failed to evaluate condition" in record.getMessage()
    ]


def _nested(field: str, clear_session: bool) -> dict[str, Any]:
    return {"arguments": {**ROUTE, field: json.dumps({**HANDOFF, "clear_session": clear_session})}}


@pytest.mark.parametrize("field", ["arguments", "args"])
@pytest.mark.parametrize(
    ("clear_session", "blocked"),
    [(False, False), (True, True)],
    ids=["in-place", "clear"],
)
@pytest.mark.asyncio
async def test_nested_json_string_arguments_reach_conditions_parsed(
    db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
    field: str,
    clear_session: bool,
    blocked: bool,
) -> None:
    """The wrapper parses the nested string once; conditions read the dispatched dict."""
    caplog.set_level(logging.ERROR)

    response = await _evaluate(db, _call_tool_event(_nested(field, clear_session)))

    assert (CLAIMED_TASKS_REASON in (response.reason or "")) is blocked
    assert _condition_errors(caplog) == []


@pytest.mark.parametrize("field", ["arguments", "args"])
def test_hook_normalization_parses_nested_string_in_one_pass(field: str) -> None:
    """Normalization alone yields the target dict, without relying on a second pass."""
    event = _call_tool_event(_nested(field, clear_session=False))

    assert event.data["tool_input"]["arguments"] == {**HANDOFF, "clear_session": False}


@pytest.mark.parametrize(
    ("tool_input", "field", "code"),
    [
        ({**ROUTE, "arguments": "{not-json"}, "arguments", "invalid_json"),
        ({**ROUTE, "arguments": "[1, 2]"}, "arguments", "non_object_json"),
        ({"args": "{not-json"}, "args", "invalid_json"),
        (
            {"arguments": {**ROUTE, "arguments": "{not-json"}},
            "arguments.arguments",
            "invalid_json",
        ),
        ({**ROUTE, "arguments": ["task"]}, "arguments", "non_object"),
        (
            {**ROUTE, "arguments": {}, "args": {"clear_session": True}},
            "arguments,args",
            "ambiguous_alias",
        ),
    ],
    ids=[
        "routed-invalid-json",
        "routed-non-object-json",
        "unrouted-invalid-json",
        "nested-invalid-json",
        "routed-list",
        "both-aliases",
    ],
)
@pytest.mark.asyncio
async def test_refused_arguments_reach_conditions_as_typed_refusal(
    db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
    tool_input: dict[str, Any],
    field: str,
    code: str,
) -> None:
    """Input the proxy refuses is visible to conditions without a raw-string crash."""
    caplog.set_level(logging.ERROR)
    event = _call_tool_event(tool_input)

    rule_input = TemplatingMixin._rule_tool_input(event)
    await _evaluate(db, event)

    assert rule_input["call_tool_arguments_error"] == {"field": field, "code": code}
    assert "arguments" not in rule_input
    assert "args" not in rule_input
    assert _condition_errors(caplog) == []
