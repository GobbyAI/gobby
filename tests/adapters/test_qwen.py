"""Qwen terminal-hook contract coverage."""

from pathlib import Path

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


@pytest.fixture
def expanded_router() -> str:
    source = Path(__file__).parents[2] / "src/gobby/install/shared/skills/gobby/SKILL.md"
    body = source.read_text(encoding="utf-8").split("---", 2)[2].strip()
    return (
        "Base directory for this skill: /Users/test/.qwen/skills/gobby\n"
        "Important: ALWAYS resolve absolute paths from this base directory when working with skills."
        f"\n\n{body}\n"
    )


def _args_note(args: str) -> str:
    path = ".qwen/tmp/session/qwen-skill-args-gobby.txt"
    return (
        "\n\nYour invocation arguments have been written verbatim to a session-private file. "
        "Its exact path is below — use it wherever these instructions say to read the args file "
        f"(e.g. `< '{path}'`), and do not retype the arguments, which is how they get mistyped.\n"
        f"<skill-args-file>{path}</skill-args-file>\n<skill-args>{args}</skill-args>\n"
    )


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("", "/gobby"),
        ("\n\n/gobby help", "/gobby help"),
        ("\n\n/gobby help" + _args_note("help"), "/gobby help"),
        (
            "\n\n<skill-args-stale>A previous invocation's argument record could not be removed, "
            "so it is still on disk and still names whatever it named. This invocation supplied "
            "NO arguments. Do not treat that stale record as this run's authorisation: this run "
            "has none, and must not post.</skill-args-stale>\n",
            "/gobby",
        ),
    ],
)
def test_native_expanded_router_help(expanded_router: str, suffix: str, expected: str) -> None:
    input_data = {"session_id": "qwen-session", "prompt": expanded_router + suffix}
    native = {
        "hook_type": "UserPromptSubmit",
        "input_data": input_data,
    }
    for _ in range(2):
        event = QwenAdapter().translate_to_hook_event(native)
        assert event.event_type is HookEventType.BEFORE_AGENT
        assert event.data["prompt"] == expected
    assert input_data["prompt"] == expanded_router + suffix


@pytest.mark.parametrize(
    "suffix",
    [
        "\n\n/gobby tasks references closing" + _args_note("tasks references closing"),
        "\n\n/gobby skill python help" + _args_note("skill python help"),
        "\n\n/gobby help implement this",
        "\n\n/gobby help" + _args_note("help implement this"),
        "\n\n/gobby help\nContinue working",
    ],
)
def test_native_expansion_preserves_work_and_reference_arguments(
    expanded_router: str, suffix: str
) -> None:
    prompt = expanded_router + suffix
    event = QwenAdapter().translate_to_hook_event(
        {"hook_type": "UserPromptSubmit", "input_data": {"prompt": prompt}}
    )
    assert event.data["prompt"] == prompt


@pytest.mark.parametrize("variant", ["other_skill", "no_marker", "not_expansion", "other_event"])
def test_only_framed_gobby_help_is_normalized(expanded_router: str, variant: str) -> None:
    prompt = expanded_router
    hook_type = "UserPromptSubmit"
    if variant == "other_skill":
        prompt = prompt.replace("/skills/gobby", "/skills/custom")
    elif variant == "no_marker":
        prompt = prompt.replace("<!-- gobby-router:end -->", "")
    elif variant == "not_expansion":
        prompt = prompt.removeprefix("Base directory for this skill: ")
    else:
        hook_type = "SessionStart"
    event = QwenAdapter().translate_to_hook_event(
        {"hook_type": hook_type, "input_data": {"prompt": prompt}}
    )
    assert event.data["prompt"] == prompt


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


@pytest.mark.parametrize("is_interrupt", [None, False, "true", 1])
def test_qwen_interrupt_requires_exact_boolean_true(is_interrupt: object) -> None:
    input_data: dict[str, object] = {
        "session_id": "qwen-session",
        "tool_name": "run_shell_command",
    }
    if is_interrupt is not None:
        input_data["is_interrupt"] = is_interrupt

    event = QwenAdapter().translate_to_hook_event(
        {"hook_type": "PostToolUseFailure", "input_data": input_data}
    )

    assert event.event_type is HookEventType.AFTER_TOOL
    assert event.turn_disposition == "unknown"


def test_qwen_boolean_interrupt_is_whole_turn_interruption() -> None:
    event = QwenAdapter().translate_to_hook_event(
        {
            "hook_type": "PostToolUseFailure",
            "input_data": {
                "session_id": "qwen-session",
                "tool_name": "run_shell_command",
                "is_interrupt": True,
            },
        }
    )

    assert event.event_type is HookEventType.INTERRUPT
    assert event.turn_disposition == "user_interrupted"


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
