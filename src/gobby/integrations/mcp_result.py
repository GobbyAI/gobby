"""Normalize MCP SDK tool result envelopes."""

from __future__ import annotations

import json
from typing import Any


class MCPToolResultError(RuntimeError):
    """An MCP tool returned an in-band error result."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def parse_mcp_tool_result(result: Any) -> Any:
    """Return the domain payload from an MCP ``CallToolResult`` envelope."""
    if getattr(result, "is_error", False):
        details = [
            item.text
            for item in getattr(result, "content", [])
            if isinstance(getattr(item, "text", None), str)
        ]
        raise MCPToolResultError("\n".join(details) or "unknown error")

    structured_content = getattr(result, "structured_content", None)
    if structured_content is not None:
        return structured_content

    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return result
