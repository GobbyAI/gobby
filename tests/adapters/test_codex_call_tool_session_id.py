"""Codex regression coverage for call_tool session_id context rewrites."""

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks.events import HookResponse

pytestmark = pytest.mark.unit


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

    assert result["continue"] is True
    assert "decision" not in result
    assert "Retry this tool call" not in result.get("systemMessage", "")
    assert "resending the corrected input" not in result.get("systemMessage", "")
