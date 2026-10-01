"""Targeted tests for Codex item normalization helpers."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.codex_impl.item_normalization import (
    build_pre_tool_lifecycle_payload,
    build_tool_event_data,
    parse_mcp_arguments,
)
from gobby.hooks.normalization import normalize_tool_fields, tool_input_error

pytestmark = pytest.mark.unit

TRUNCATED = '{"server_name": "gobby-tasks", "tool_name": "close_task", "arguments": {"commit'


def test_parse_mcp_arguments_logs_debug_on_invalid_json(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="gobby.adapters.codex_impl.item_normalization"):
        parsed = parse_mcp_arguments('{"broken"')

    assert parsed == {}
    assert any("Failed to parse MCP arguments JSON" in message for message in caplog.messages)


def test_item_started_hands_truncated_arguments_to_the_hook_unchanged() -> None:
    params: dict[str, Any] = {
        "item": {
            "type": "mcpToolCall",
            "id": "item-1",
            "server": "gobby",
            "tool": "call_tool",
            "arguments": TRUNCATED,
        }
    }

    payload = build_pre_tool_lifecycle_payload(params, tool_name_map=CodexAdapter.TOOL_MAP)

    assert payload == ("mcp__gobby__call_tool", TRUNCATED)
    lifecycle_data = normalize_tool_fields({"tool_name": payload[0], "tool_input": payload[1]})
    assert tool_input_error(lifecycle_data) == {"field": "tool_input", "code": "invalid_json"}


def test_functions_exec_source_is_tool_input_not_unavailable_json() -> None:
    source = 'const out = await tools.exec_command({cmd: "git status"}); text(out);'

    data = build_tool_event_data(
        {"type": "dynamicToolCall", "id": "item-2", "tool": "exec", "arguments": source}
    )

    assert data["tool_input"] == {"arguments": source}
    assert tool_input_error(data) is None


def test_mcp_approval_forwards_truncated_arguments_and_marks_them() -> None:
    hook_event = CodexAdapter()._translate_approval_event(
        "item/mcpToolCall/requestApproval",
        {
            "threadId": "thr-mcp",
            "itemId": "item-mcp",
            "mcpToolCall": {"name": "mcp__gobby__call_tool", "arguments": TRUNCATED},
        },
    )

    assert hook_event is not None
    assert hook_event.data["toolArgs"] == TRUNCATED
    assert "tool_input" not in hook_event.data
    assert tool_input_error(hook_event.data) == {"field": "toolArgs", "code": "invalid_json"}


def test_mcp_elicitation_forwards_string_tool_params_and_marks_them() -> None:
    hook_event = CodexAdapter()._translate_approval_event(
        "mcpServer/elicitation/request",
        {
            "threadId": "thr-mcp",
            "serverName": "gobby",
            "elicitationId": "elicit-1",
            "message": 'Allow the gobby MCP server to run tool "call_tool"?',
            "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": TRUNCATED},
        },
    )

    assert hook_event is not None
    assert "tool_input" not in hook_event.data
    assert tool_input_error(hook_event.data) == {"field": "tool_input", "code": "invalid_json"}
