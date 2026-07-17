"""Qwen terminal-hook contract coverage."""

import pytest

from gobby.adapters.acp_hook_adapter import ACPHookAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.events import HookEventType, HookResponse, SessionSource

QWEN_EVENTS = {
    "SessionStart": HookEventType.SESSION_START,
    "SessionEnd": HookEventType.SESSION_END,
    "UserPromptSubmit": HookEventType.BEFORE_AGENT,
    "PreToolUse": HookEventType.BEFORE_TOOL,
    "PermissionRequest": HookEventType.PERMISSION_REQUEST,
    "PostToolUse": HookEventType.AFTER_TOOL,
    "PostToolUseFailure": HookEventType.AFTER_TOOL,
    "Stop": HookEventType.STOP,
    "StopFailure": HookEventType.STOP_FAILURE,
    "SubagentStart": HookEventType.SUBAGENT_START,
    "SubagentStop": HookEventType.SUBAGENT_STOP,
    "PreCompact": HookEventType.PRE_COMPACT,
    "PostCompact": HookEventType.POST_COMPACT,
    "Notification": HookEventType.NOTIFICATION,
    "TodoCreated": HookEventType.TASK_CREATED,
    "TodoCompleted": HookEventType.TASK_COMPLETED,
}


def test_qwen_terminal_adapter_is_not_the_acp_hook_adapter() -> None:
    assert not isinstance(QwenAdapter(), ACPHookAdapter)


@pytest.mark.parametrize(("hook_type", "event_type"), QWEN_EVENTS.items())
def test_all_current_qwen_events_translate(
    hook_type: str,
    event_type: HookEventType,
) -> None:
    event = QwenAdapter().translate_to_hook_event(
        {
            "hook_type": hook_type,
            "input_data": {
                "session_id": "qwen-session",
                "cwd": "/tmp/project",
                "phase": "validation",
            },
        }
    )

    assert event.event_type is event_type
    assert event.source is SessionSource.QWEN
    assert event.session_id == "qwen-session"
    assert event.data["phase"] == "validation"


@pytest.mark.parametrize(
    ("hook_type", "is_failure"),
    [("PostToolUse", False), ("PostToolUseFailure", True)],
)
def test_qwen_tool_outcome_hook_names_are_definitive(
    hook_type: str,
    is_failure: bool,
) -> None:
    event = QwenAdapter().translate_to_hook_event(
        {
            "hook_type": hook_type,
            "input_data": {
                "session_id": "qwen-session",
                "tool_name": "run_shell_command",
                "tool_response": {"output": "done"},
            },
        }
    )

    assert event.data["tool_name"] == "Bash"
    assert event.metadata["is_failure"] is is_failure


def test_qwen_pre_tool_denial_uses_permission_decision_channel() -> None:
    result = QwenAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason="policy"),
        hook_type="PreToolUse",
    )

    assert result == {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "policy",
        },
    }


def test_qwen_permission_request_uses_structured_decision_channel() -> None:
    result = QwenAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason="policy"),
        hook_type="PermissionRequest",
    )

    assert result == {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "policy",
                "interrupt": True,
            },
        },
    }


@pytest.mark.parametrize("hook_type", ["Stop", "SubagentStop", "TodoCreated", "TodoCompleted"])
def test_qwen_validation_blocks_use_top_level_decision(
    hook_type: str,
) -> None:
    result = QwenAdapter().translate_from_hook_response(
        HookResponse(decision="block", reason="keep working"),
        hook_type=hook_type,
    )

    assert result == {
        "continue": True,
        "decision": "block",
        "reason": "keep working",
    }


@pytest.mark.parametrize("hook_type", ["Stop", "SubagentStop", "TodoCreated", "TodoCompleted"])
def test_qwen_validation_allows_use_top_level_decision(
    hook_type: str,
) -> None:
    result = QwenAdapter().translate_from_hook_response(
        HookResponse(decision="allow", reason="complete"),
        hook_type=hook_type,
    )

    assert result == {
        "continue": True,
        "decision": "allow",
        "reason": "complete",
    }


@pytest.mark.parametrize(
    "hook_type",
    [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "SubagentStart",
        "PreCompact",
        "PostCompact",
        "Notification",
    ],
)
def test_qwen_context_uses_event_specific_additional_context(hook_type: str) -> None:
    result = QwenAdapter().translate_from_hook_response(
        HookResponse(decision="allow", context="Gobby context"),
        hook_type=hook_type,
    )

    output = result["hookSpecificOutput"]
    assert output["hookEventName"] == hook_type
    assert output["additionalContext"] == "Gobby context"


def test_qwen_session_start_routes_banner_once() -> None:
    banner = "Gobby Session ID: #42 (uuid-123)"
    result = QwenAdapter().translate_from_hook_response(
        HookResponse(
            decision="allow",
            system_message=banner,
            metadata={
                "session_id": "uuid-123",
                "session_ref": "#42",
                "external_id": "qwen-ext-id",
                "_first_hook_for_session": True,
                "project_id": "proj-xyz",
            },
        ),
        hook_type="SessionStart",
    )

    assert "systemMessage" not in result
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.count(banner) == 1
    assert "qwen-ext-id" in context
    assert "proj-xyz" in context
