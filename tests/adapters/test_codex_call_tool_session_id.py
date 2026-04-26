"""Codex regression coverage for call_tool session_id context rewrites."""

import logging
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.hook_manager import HookManager

pytestmark = pytest.mark.unit


def _prepare_manager_for_before_tool(manager: HookManager) -> None:
    manager._event_handlers.get_handler.return_value = MagicMock(
        return_value=HookResponse(decision="allow")
    )
    manager._session_lookup.resolve.return_value = None
    manager._workflow_handler.handle.return_value = HookResponse(decision="allow")
    manager._enricher.enrich = MagicMock()


def _assert_no_retry_block(result: dict) -> None:
    assert result["continue"] is True
    assert "decision" not in result
    rendered = repr(result)
    assert "Retry this tool call" not in rendered
    assert "resending the corrected input" not in rendered
    assert "corrected input" not in rendered
    assert "Do not add, remove, or rename fields" not in rendered


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
        metadata={
            "_normalized_tool_name": "mcp__gobby__call_tool",
            "_raw_tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": "get_session",
                "arguments": {"name": "brevity", "session_id": "#3"},
            },
        },
    )

    assert response.modified_input is None
    result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

    assert result == {"continue": True}
    _assert_no_retry_block(result)


def test_workflow_modified_input_for_codex_falls_through_with_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = CodexHooksAdapter()
    context = "Hypothetical Codex-targeting workflow rule rewrote a tool input."
    response = HookResponse(
        decision="allow",
        context=context,
        modified_input={"command": "echo hello"},
    )

    with caplog.at_level(logging.DEBUG, logger="gobby.adapters.codex_impl.hooks_adapter"):
        result = adapter.translate_from_hook_response(response, hook_type="PreToolUse")

    _assert_no_retry_block(result)
    assert result["continue"] is True
    assert context in result["systemMessage"]
    debug_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "gobby.adapters.codex_impl.hooks_adapter"
        and record.levelno == logging.DEBUG
        and record.getMessage().startswith("Codex PreToolUse hook returned modified_input")
    ]
    assert len(debug_messages) == 1


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


def test_user_repro_get_skill_call_tool_with_nested_uuid(
    manager_with_mocks: HookManager,
    make_before_tool_event: Callable[[dict], HookEvent],
) -> None:
    """Exercise HookManager.handle with mocked session/health gates, then Codex translation."""
    manager = manager_with_mocks
    _prepare_manager_for_before_tool(manager)
    event = make_before_tool_event(
        {
            "server_name": "gobby-skills",
            "tool_name": "get_skill",
            "arguments": {
                "name": "brevity",
                "session_id": "0c64f1e4-ef3e-46ee-8d5e-ad322e04b93c",
            },
        }
    )

    response = manager.handle(event)
    result = CodexHooksAdapter().translate_from_hook_response(response, hook_type="PreToolUse")

    assert response.modified_input is None
    _assert_no_retry_block(result)


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
