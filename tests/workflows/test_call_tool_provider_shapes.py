"""Provider call_tool wrapper shapes against the schema-lease gate.

Every provider hook path must hand the rule engine the call_tool target the proxy
dispatches, so schema-lookup leases and schema-before-call checks
checks, and the gate's block reason all use one ``server:tool`` key.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

SESSION_ID = "33333333-3333-4333-8333-333333333333"
EXTERNAL_SESSION_ID = "44444444-4444-4444-8444-444444444444"
# The isolated test hub seeds this local machine row.
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
SERVER = "gobby-tasks"
TOOL = "add_label"
TARGET_ARGUMENTS = {"task_id": "#1", "label": "ready"}


def _escaped_json(value: dict[str, Any]) -> str:
    return json.dumps(value).replace('"', '\\"')


# Wrapper shapes the proxy's canonicalize_call_tool_wrapper accepts for one target.
CALL_TOOL_SHAPES: dict[str, dict[str, Any]] = {
    "top-level-arguments-dict": {
        "server_name": SERVER,
        "tool_name": TOOL,
        "arguments": TARGET_ARGUMENTS,
    },
    "top-level-arguments-json": {
        "server_name": SERVER,
        "tool_name": TOOL,
        "arguments": json.dumps(TARGET_ARGUMENTS),
    },
    "top-level-args-json": {
        "server_name": SERVER,
        "tool_name": TOOL,
        "args": json.dumps(TARGET_ARGUMENTS),
        "preflight_enabled": True,
    },
    "nested-route-in-arguments": {
        "arguments": {"server_name": SERVER, "tool_name": TOOL, "arguments": TARGET_ARGUMENTS},
    },
    "nested-route-in-args-json": {
        "args": json.dumps({"server_name": SERVER, "tool_name": TOOL, "args": TARGET_ARGUMENTS}),
    },
    "split-top-and-nested-route": {
        "server_name": SERVER,
        "arguments": {"tool_name": TOOL, **TARGET_ARGUMENTS},
    },
    "escaped-json-nested-route": {
        "arguments": _escaped_json(
            {"server_name": SERVER, "tool_name": TOOL, "arguments": TARGET_ARGUMENTS}
        ),
    },
}

# Payloads the proxy cannot route: the Droid #13650 shape and a half route.
ROUTELESS_SHAPES: dict[str, dict[str, Any]] = {
    "args-without-route": {
        "args": json.dumps({"name": "gobby", "path": "references/memory/overview.md"}),
        "preflight_enabled": True,
    },
    "tool-without-server": {"tool_name": TOOL, "arguments": TARGET_ARGUMENTS},
}

BEFORE = "before"
AFTER = "after"

ProviderEventBuilder = Callable[[str, str, dict[str, Any]], HookEvent]


def _claude_event(phase: str, proxy_tool: str, tool_input: dict[str, Any]) -> HookEvent:
    return ClaudeCodeAdapter().translate_to_hook_event(
        {
            "hook_type": "pre-tool-use" if phase == BEFORE else "post-tool-use",
            "input_data": {
                "session_id": EXTERNAL_SESSION_ID,
                "tool_name": f"mcp__gobby__{proxy_tool}",
                "tool_input": tool_input,
            },
        }
    )


def _codex_hooks_event(phase: str, proxy_tool: str, tool_input: dict[str, Any]) -> HookEvent:
    event = CodexHooksAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse" if phase == BEFORE else "PostToolUse",
            "input_data": {
                "session_id": EXTERNAL_SESSION_ID,
                "tool_name": f"mcp__gobby__{proxy_tool}",
                "tool_input": tool_input,
            },
        }
    )
    assert event is not None
    return event


def _droid_event(phase: str, proxy_tool: str, tool_input: dict[str, Any]) -> HookEvent:
    hook_type = "PreToolUse" if phase == BEFORE else "PostToolUse"
    return DroidAdapter().translate_to_hook_event(
        {
            "hook_type": hook_type,
            "input_data": {
                "hook_event_name": hook_type,
                "session_id": EXTERNAL_SESSION_ID,
                "tool_name": f"gobby___{proxy_tool}",
                "tool_input": tool_input,
            },
        }
    )


def _grok_event(phase: str, proxy_tool: str, tool_input: dict[str, Any]) -> HookEvent:
    tool_name = f"gobby__{proxy_tool}"
    return GrokAdapter().translate_to_hook_event(
        {
            "hook_type": "pre_tool_use" if phase == BEFORE else "post_tool_use",
            "input_data": {
                "sessionId": EXTERNAL_SESSION_ID,
                "toolName": tool_name,
                "toolInput": {"tool_name": tool_name, "tool_input": tool_input},
            },
        }
    )


def _agy_event(phase: str, proxy_tool: str, tool_input: dict[str, Any]) -> HookEvent:
    hook_type = "PreToolUse" if phase == BEFORE else "PostToolUse"
    return AgyAdapter().translate_to_hook_event(
        {
            "source": "agy",
            "hook_type": hook_type,
            "input_data": {
                "hookEventName": hook_type,
                "conversationId": EXTERNAL_SESSION_ID,
                "toolCall": {
                    "name": "call_mcp_tool",
                    "args": {
                        "ServerName": "gobby",
                        "ToolName": proxy_tool,
                        "Arguments": json.dumps(tool_input),
                    },
                },
            },
        }
    )


def _unnormalized_event(phase: str, proxy_tool: str, tool_input: dict[str, Any]) -> HookEvent:
    """Events that reach the engine without adapter normalization (rule context path)."""
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL if phase == BEFORE else HookEventType.AFTER_TOOL,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": f"mcp__gobby__{proxy_tool}", "tool_input": dict(tool_input)},
    )


PROVIDERS: dict[str, ProviderEventBuilder] = {
    "claude": _claude_event,
    "codex-hooks": _codex_hooks_event,
    "droid": _droid_event,
    "grok": _grok_event,
    "agy": _agy_event,
    "unnormalized": _unnormalized_event,
}


def _bind_session(event: HookEvent) -> HookEvent:
    event.metadata["_platform_session_id"] = SESSION_ID
    return event


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    from gobby.storage.definitions.rules import RuleDefinitionManager

    RuleDefinitionManager(temp_db).create(
        name="synthetic-route-observer",
        definition_json={
            "event": "before_tool",
            "effects": [
                {
                    "type": "set_variable",
                    "variable": "observed_route",
                    "value": "{{ tool_input.get('server_name', '') }}:{{ tool_input.get('tool_name', '') }}",
                }
            ],
        },
        tags=["default"],
    )
    return RuleEngine(temp_db)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", CALL_TOOL_SHAPES)
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_provider_call_shapes_normalize_before_rule_evaluation(
    engine: RuleEngine, provider: str, shape: str
) -> None:
    variables: dict[str, Any] = {}
    event = _bind_session(PROVIDERS[provider](BEFORE, "call_tool", CALL_TOOL_SHAPES[shape]))
    response = await engine.evaluate(event, SESSION_ID, variables)
    assert response.decision == "allow"
    assert variables["observed_route"] == f"{SERVER}:{TOOL}"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ROUTELESS_SHAPES)
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_missing_route_is_available_as_empty_fields(
    engine: RuleEngine, provider: str, shape: str
) -> None:
    variables: dict[str, Any] = {}
    event = _bind_session(PROVIDERS[provider](BEFORE, "call_tool", ROUTELESS_SHAPES[shape]))
    response = await engine.evaluate(event, SESSION_ID, variables)
    assert response.decision == "allow"
    expected_tool = ROUTELESS_SHAPES[shape].get("tool_name", "")
    assert variables["observed_route"] == f":{expected_tool}"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", CALL_TOOL_SHAPES)
async def test_codex_approval_normalizes_the_same_route(engine: RuleEngine, shape: str) -> None:
    hook_manager = MagicMock()
    hook_manager.handle.return_value = HookResponse(decision="allow")
    adapter = CodexAdapter(hook_manager=hook_manager)
    await adapter.handle_approval_request(
        "item/mcpToolCall/requestApproval",
        {
            "threadId": "synthetic-thread",
            "itemId": "synthetic-item",
            "name": "mcp__gobby__call_tool",
            "arguments": CALL_TOOL_SHAPES[shape],
        },
    )
    event = _bind_session(hook_manager.handle.call_args.args[0])
    variables: dict[str, Any] = {}
    response = await engine.evaluate(event, SESSION_ID, variables)
    assert response.decision == "allow"
    assert variables["observed_route"] == f"{SERVER}:{TOOL}"
