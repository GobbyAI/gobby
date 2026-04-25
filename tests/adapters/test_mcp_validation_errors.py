"""Shared CLI adapter expectations for MCP validation errors."""

from __future__ import annotations

import json

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.adapters.gemini import GeminiAdapter
from gobby.adapters.qwen import QwenAdapter
from gobby.hooks.events import HookResponse

pytestmark = pytest.mark.unit


def _mcp_validation_response() -> HookResponse:
    context = json.dumps(
        {
            "success": False,
            "error_code": "invalid_arguments",
            "validation_errors": ["Unknown parameter 'server_name'"],
            "schema": {"type": "object", "properties": {"title": {"type": "string"}}},
        }
    )
    return HookResponse(
        decision="block",
        reason="Invalid arguments: Unknown parameter 'server_name'",
        context=context,
        modified_input={"arguments": {"title": "Fix", "server_name": "gobby-tasks"}},
        auto_approve=True,
        metadata={
            "_normalized_tool_name": "mcp__gobby__call_tool",
            "_raw_tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "Fix", "server_name": "gobby-tasks"},
            },
        },
    )


def test_claude_pre_tool_use_mcp_validation_error_has_no_updated_input() -> None:
    result = ClaudeCodeAdapter().translate_from_hook_response(
        _mcp_validation_response(),
        hook_type="pre-tool-use",
    )

    hook_output = result["hookSpecificOutput"]
    assert hook_output["permissionDecision"] == "deny"
    assert "updatedInput" not in hook_output
    assert "invalid_arguments" in hook_output["additionalContext"]


def test_claude_permission_request_mcp_validation_error_has_no_updated_input() -> None:
    result = ClaudeCodeAdapter().translate_from_hook_response(
        _mcp_validation_response(),
        hook_type="permission-request",
    )

    decision = result["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    assert "updatedInput" not in decision


def test_codex_mcp_validation_error_has_no_retry_payload() -> None:
    result = CodexHooksAdapter().translate_from_hook_response(
        _mcp_validation_response(),
        hook_type="PreToolUse",
    )

    assert result["decision"] == "block"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Retry this tool call" not in result.get("systemMessage", "")
    assert "corrected input" not in result.get("systemMessage", "")


def test_gemini_mcp_validation_error_is_plain_block_context() -> None:
    result = GeminiAdapter().translate_from_hook_response(
        _mcp_validation_response(),
        hook_type="BeforeTool",
    )

    assert result["decision"] == "block"
    assert "updatedInput" not in result
    assert "Retry this tool call" not in str(result)
    assert "invalid_arguments" in result["hookSpecificOutput"]["additionalContext"]


def test_qwen_mcp_validation_error_is_plain_block_context() -> None:
    result = QwenAdapter().translate_from_hook_response(
        _mcp_validation_response(),
        hook_type="BeforeTool",
    )

    assert result["decision"] == "block"
    assert "updatedInput" not in result
    assert "Retry this tool call" not in str(result)
    assert "invalid_arguments" in result["hookSpecificOutput"]["additionalContext"]
