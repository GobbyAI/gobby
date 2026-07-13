"""Shared result contract for hook-dispatched MCP calls."""

from typing import Any


def mcp_call_succeeded(result: Any) -> bool:
    """Return whether an MCP call result represents success.

    Successful internal results may have their ``success`` key stripped, and
    external MCP servers return non-dict ``CallToolResult`` objects. Only a
    missing result or an explicit ``success: false`` mapping is a failure.
    """
    return result is not None and not (isinstance(result, dict) and result.get("success") is False)
