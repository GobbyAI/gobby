"""Tests for shared MCP tool-result normalization."""

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.integrations.mcp_result import MCPToolResultError, parse_mcp_tool_result


def test_parse_mcp_tool_result_prefers_structured_content() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text='{"source":"text"}')],
        structured_content={"source": "structured"},
        is_error=False,
    )

    assert parse_mcp_tool_result(result) == {"source": "structured"}


def test_parse_mcp_tool_result_decodes_first_text_content() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text='{"issues":[{"id":"lin-1"}]}')],
        is_error=False,
    )

    assert parse_mcp_tool_result(result) == {"issues": [{"id": "lin-1"}]}


def test_parse_mcp_tool_result_raises_for_error_envelope() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="permission denied")],
        structured_content={"ignored": True},
        is_error=True,
    )

    with pytest.raises(MCPToolResultError, match="permission denied"):
        parse_mcp_tool_result(result)
