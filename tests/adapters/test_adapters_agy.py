"""AGY hook adapter tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.hooks.events import HookEventType, HookResponse, SessionSource

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("hook_type", "expected_type"),
    [
        ("PreInvocation", HookEventType.BEFORE_AGENT),
        ("PreToolUse", HookEventType.BEFORE_TOOL),
        ("PostToolUse", HookEventType.AFTER_TOOL),
        ("PostInvocation", HookEventType.AFTER_AGENT),
        ("Stop", HookEventType.STOP),
    ],
)
def test_translate_to_hook_event_maps_agy_hooks(
    hook_type: str,
    expected_type: HookEventType,
) -> None:
    adapter = AgyAdapter()

    event = adapter.translate_to_hook_event(
        {
            "source": "agy",
            "hook_type": hook_type,
            "input_data": {
                "hook_event_name": hook_type,
                "session_id": "agy-session-123",
                "cwd": "/repo",
            },
        }
    )

    assert event.event_type is expected_type
    assert event.source is SessionSource.AGY
    assert event.session_id == "agy-session-123"
    assert event.cwd == "/repo"


def test_translate_to_hook_event_accepts_direct_agy_payload() -> None:
    event = AgyAdapter().translate_to_hook_event(
        {
            "hook_event_name": "PreInvocation",
            "session_id": "agy-direct-123",
            "cwd": "/workspace",
            "prompt": "implement the task",
        }
    )

    assert event.event_type is HookEventType.BEFORE_AGENT
    assert event.session_id == "agy-direct-123"
    assert event.cwd == "/workspace"
    assert event.data["prompt"] == "implement the task"


def test_pre_tool_use_normalizes_agy_shell_tool_name() -> None:
    event = AgyAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "hook_event_name": "PreToolUse",
                "session_id": "agy-tool-123",
                "tool_name": "run_shell_command",
                "tool_input": {"command": "pwd"},
            },
        }
    )

    assert event.event_type is HookEventType.BEFORE_TOOL
    assert event.data["tool_name"] == "Bash"
    assert event.metadata["original_tool_name"] == "run_shell_command"
    assert event.metadata["normalized_tool_name"] == "Bash"


def test_pre_tool_use_allow_response_is_compact() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="allow"),
        hook_type="PreToolUse",
    )

    assert result == {"decision": "allow"}


def test_pre_tool_use_block_response_becomes_agy_deny() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason="policy blocked this command"),
        hook_type="PreToolUse",
    )

    assert result == {"decision": "deny", "reason": "policy blocked this command"}


def test_pre_tool_use_ask_response_can_rewrite_input() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(
            decision="ask",
            reason="needs confirmation",
            modified_input={"command": "pwd"},
        ),
        hook_type="PreToolUse",
    )

    assert result == {
        "decision": "ask",
        "reason": "needs confirmation",
        "overwrite": {"command": "pwd"},
    }


def test_stop_block_response_continues_the_agent_loop() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason="stay active"),
        hook_type="Stop",
    )

    assert result == {"decision": "continue", "reason": "stay active"}


def test_stop_allow_response_is_empty() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="allow"),
        hook_type="Stop",
    )

    assert result == {}


def test_pre_invocation_context_becomes_inject_steps() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(
            decision="allow",
            context="ephemeral note",
            system_message="user note",
        ),
        hook_type="PreInvocation",
    )

    assert result == {
        "injectSteps": [
            {"ephemeralMessage": "ephemeral note"},
            {"userMessage": "user note"},
        ]
    }


def test_post_tool_use_response_is_empty() -> None:
    result = AgyAdapter().translate_from_hook_response(
        HookResponse(decision="allow", reason="unused"),
        hook_type="PostToolUse",
    )

    assert result == {}


def test_handle_native_uses_agy_source_and_compact_response() -> None:
    hook_manager = MagicMock()
    hook_manager.handle.return_value = HookResponse(decision="allow")
    native_event = {
        "hook_type": "PreToolUse",
        "source": "agy",
        "input_data": {
            "hook_event_name": "PreToolUse",
            "session_id": "agy-handle-123",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "pwd"},
        },
    }

    result = AgyAdapter().handle_native(native_event, hook_manager)

    assert result == {"decision": "allow"}
    hook_event = hook_manager.handle.call_args.args[0]
    assert hook_event.source is SessionSource.AGY
    assert hook_event.event_type is HookEventType.BEFORE_TOOL
