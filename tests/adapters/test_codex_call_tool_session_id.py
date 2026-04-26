"""Codex regression coverage for call_tool session_id context rewrites."""

import logging

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks.events import HookResponse

pytestmark = pytest.mark.unit


def _assert_no_retry_block(result: dict) -> None:
    assert result["continue"] is True
    assert "decision" not in result
    assert "Retry this tool call" not in result.get("systemMessage", "")
    assert "resending the corrected input" not in result.get("systemMessage", "")
    assert "corrected input" not in result.get("systemMessage", "")


def test_wrapper_context_only_session_resolution_does_not_emit_retry_block() -> None:
    adapter = CodexHooksAdapter()
    response = HookResponse(
        decision="allow",
        context="Wrapper session context resolved.",
        metadata={
            "_normalized_tool_name": "mcp__gobby__call_tool",
            "_raw_tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": "get_session",
                "arguments": {},
                "session_id": "#3",
            },
        },
    )

    result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

    _assert_no_retry_block(result)


def test_nested_arguments_session_resolution_does_not_emit_retry_block() -> None:
    adapter = CodexHooksAdapter()
    response = HookResponse(
        decision="allow",
        context="Nested session reference resolved.",
        metadata={
            "_normalized_tool_name": "mcp__gobby__call_tool",
            "_raw_tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": "get_session",
                "arguments": {"session_id": "#3"},
            },
        },
    )

    result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

    _assert_no_retry_block(result)
    assert "Nested session reference resolved." in result["systemMessage"]


def test_workflow_modified_input_falls_through_with_context_in_system_message() -> None:
    adapter = CodexHooksAdapter()
    response = HookResponse(
        decision="allow",
        context="Bare python is not allowed.",
        modified_input={"command": "uv run python hello.py"},
        auto_approve=True,
    )

    result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

    _assert_no_retry_block(result)
    assert "Bare python is not allowed." in result["systemMessage"]
    assert "uv run python hello.py" not in result["systemMessage"]


def test_uuid_session_id_user_repro_does_not_emit_retry_block() -> None:
    adapter = CodexHooksAdapter()
    response = HookResponse(
        decision="allow",
        context="Skill fetch context.",
        metadata={
            "_normalized_tool_name": "mcp__gobby__call_tool",
            "_raw_tool_input": {
                "server_name": "gobby-skills",
                "tool_name": "get_skill",
                "arguments": {
                    "name": "brevity",
                    "session_id": "33333333-3333-4333-8333-333333333333",
                },
            },
        },
    )

    result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

    _assert_no_retry_block(result)
    assert "Skill fetch context." in result["systemMessage"]


def test_modified_input_allow_path_logs_debug_without_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = CodexHooksAdapter()
    response = HookResponse(
        decision="allow",
        context="Rewrite will be applied server-side.",
        modified_input={"command": "uv run python hello.py"},
        auto_approve=True,
    )

    with caplog.at_level(logging.DEBUG, logger="gobby.adapters.codex_impl.hooks_adapter"):
        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

    _assert_no_retry_block(result)
    assert "Codex PreToolUse hook returned modified_input" in (caplog.text)
