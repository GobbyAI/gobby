"""Provider call_tool wrapper shapes against the schema-lease gate.

Every provider hook path must hand the rule engine the call_tool target the proxy
dispatches, so ``track-schema-lookup`` leases, ``require-current-context-schema-before-call``
checks, and the gate's block reason all use one ``server:tool`` key.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.droid import DroidAdapter
from gobby.adapters.grok import GrokAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.blocked_tool_recovery import recovery_directive_suffix
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from gobby.workflows.sync_variables import sync_bundled_variables
from gobby.workflows.variable_defaults import merge_unloaded_variable_defaults

pytestmark = pytest.mark.unit

SESSION_ID = "33333333-3333-4333-8333-333333333333"
EXTERNAL_SESSION_ID = "44444444-4444-4444-8444-444444444444"
# The isolated test hub seeds this local machine row.
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
GATE_RULE = "require-current-context-schema-before-call"
TRACK_RULE = "track-schema-lookup"
SERVER = "gobby-tasks"
TOOL = "add_label"
LEASE = f"{SERVER}:{TOOL}"
TARGET_ARGUMENTS = {"task_id": "#1", "label": "ready"}
KNOWN_ROUTE_HINT = f"get_tool_schema(server_name='{SERVER}', tool_name='{TOOL}')"
ROUTELESS_HINT = "Run call_tool again with server_name and tool_name as top-level parameters"


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


def _qwen_event(phase: str, proxy_tool: str, tool_input: dict[str, Any]) -> HookEvent:
    return QwenAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse" if phase == BEFORE else "PostToolUse",
            "input_data": {
                "session_id": EXTERNAL_SESSION_ID,
                "tool_name": f"mcp_gobby_{proxy_tool}",
                "tool_input": tool_input,
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
    "qwen": _qwen_event,
    "agy": _agy_event,
    "unnormalized": _unnormalized_event,
}


def _bind_session(event: HookEvent) -> HookEvent:
    event.metadata["_platform_session_id"] = SESSION_ID
    return event


@pytest.fixture
def engine(temp_db: HubDatabase) -> RuleEngine:
    _install_schema_lease_rules(temp_db)
    return RuleEngine(temp_db)


def _install_schema_lease_rules(db: HubDatabase) -> None:
    sync_bundled_rules(db, get_bundled_rules_path())
    with db.transaction() as conn:
        conn.execute("UPDATE rule_definitions SET source = 'installed', enabled = FALSE")
        conn.execute(
            "UPDATE rule_definitions SET enabled = TRUE WHERE name IN (%s, %s)",
            (GATE_RULE, TRACK_RULE),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", CALL_TOOL_SHAPES)
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_lease_from_track_schema_lookup_satisfies_gate_for_every_shape(
    engine: RuleEngine, provider: str, shape: str
) -> None:
    build = PROVIDERS[provider]
    variables: dict[str, Any] = {"enforce_tool_schema_check": True, "unlocked_tools": []}

    unleased = await engine.evaluate(
        _bind_session(build(BEFORE, "call_tool", CALL_TOOL_SHAPES[shape])), SESSION_ID, variables
    )
    assert unleased.decision == "block"
    assert KNOWN_ROUTE_HINT in (unleased.reason or "")
    assert "?" not in (unleased.reason or "")

    schema_lookup = build(AFTER, "get_tool_schema", {"server_name": SERVER, "tool_name": TOOL})
    tracked = await engine.evaluate(_bind_session(schema_lookup), SESSION_ID, variables)
    assert tracked.decision == "allow"
    assert variables["unlocked_tools"] == [LEASE]

    leased = await engine.evaluate(
        _bind_session(build(BEFORE, "call_tool", CALL_TOOL_SHAPES[shape])), SESSION_ID, variables
    )
    assert leased.decision == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ROUTELESS_SHAPES)
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_routeless_call_stays_blocked_with_actionable_reason(
    engine: RuleEngine, provider: str, shape: str
) -> None:
    variables: dict[str, Any] = {
        "enforce_tool_schema_check": True,
        "unlocked_tools": ["gobby-skills:get_skill_file", LEASE],
    }
    event = PROVIDERS[provider](BEFORE, "call_tool", ROUTELESS_SHAPES[shape])

    result = await engine.evaluate(_bind_session(event), SESSION_ID, variables)

    reason = result.reason or ""
    assert result.decision == "block"
    assert "?" not in reason
    assert "get_tool_schema" not in reason
    assert ROUTELESS_HINT in reason
    assert ROUTELESS_HINT in recovery_directive_suffix(reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", CALL_TOOL_SHAPES)
async def test_codex_app_server_approval_shares_the_hook_lease(
    engine: RuleEngine, shape: str
) -> None:
    hook_manager = MagicMock()
    hook_manager.handle.return_value = HookResponse(decision="allow")
    adapter = CodexAdapter(hook_manager=hook_manager)
    await adapter.handle_approval_request(
        "item/mcpToolCall/requestApproval",
        {
            "threadId": "thr-lease",
            "itemId": "item-lease",
            "name": "mcp__gobby__call_tool",
            "arguments": CALL_TOOL_SHAPES[shape],
        },
    )
    approval_event = hook_manager.handle.call_args.args[0]
    variables: dict[str, Any] = {"enforce_tool_schema_check": True, "unlocked_tools": []}

    blocked = await engine.evaluate(_bind_session(approval_event), SESSION_ID, variables)
    assert blocked.decision == "block"
    assert KNOWN_ROUTE_HINT in (blocked.reason or "")

    schema_lookup = _codex_hooks_event(
        AFTER, "get_tool_schema", {"server_name": SERVER, "tool_name": TOOL}
    )
    await engine.evaluate(_bind_session(schema_lookup), SESSION_ID, variables)
    allowed = await engine.evaluate(_bind_session(approval_event), SESSION_ID, variables)
    assert allowed.decision == "allow"


def _insert_fresh_session(db: HubDatabase) -> str:
    project_id = str(uuid4())
    session_id = str(uuid4())
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_id, "lease-race"))
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP)",
        (session_id, f"ext-{session_id}", LOCAL_MACHINE_ID, "claude", project_id),
    )
    return session_id


@pytest.mark.asyncio
async def test_first_call_of_a_fresh_session_is_gated_by_layered_defaults(
    temp_db: HubDatabase,
) -> None:
    """Activation race: enforcement is already on when a new session's first call arrives.

    ``enforce_tool_schema_check`` comes from the bundled variable defaults, which
    ``SessionVariableManager.get_variables`` layers onto every read and the hook handler
    merges before any rule runs, so no persisted session state has to land first.
    """
    sync_result = sync_bundled_variables(temp_db)
    assert sync_result["errors"] == []
    _install_schema_lease_rules(temp_db)
    fresh_session_id = _insert_fresh_session(temp_db)

    assert SessionVariableManager(temp_db).get_variables(fresh_session_id)[
        "enforce_tool_schema_check"
    ]
    assert merge_unloaded_variable_defaults(temp_db, fresh_session_id, {})[
        "enforce_tool_schema_check"
    ]

    handler = WorkflowHookHandler(
        rule_engine=RuleEngine(temp_db),
        config=SimpleNamespace(workflow=SimpleNamespace(enabled=True, timeout=5.0)),
    )
    first_call = _claude_event(BEFORE, "call_tool", CALL_TOOL_SHAPES["top-level-arguments-dict"])
    first_call.metadata["_platform_session_id"] = fresh_session_id

    response = await handler._evaluate_rules(first_call)

    assert response.decision == "block"
    assert KNOWN_ROUTE_HINT in (response.reason or "")
