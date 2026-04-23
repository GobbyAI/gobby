"""Shared helpers for structured block observability logs."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any


def block_tool_name(tool_name: str, input_data: Mapping[str, Any] | None = None) -> str:
    """Return a normalized tool identity for structured block logs."""
    if tool_name in {"call_tool", "mcp__gobby__call_tool"}:
        tool_input = input_data or {}
        server_name = str(tool_input.get("server_name", ""))
        mcp_tool_name = str(tool_input.get("tool_name", ""))
        if server_name and mcp_tool_name:
            return f"{server_name}:{mcp_tool_name}"
    return tool_name or "-"


def block_tool_name_from_event_data(event_data: Mapping[str, Any]) -> str:
    """Extract a normalized tool identity from hook event payload data."""
    tool_input = event_data.get("tool_input")
    normalized_input = tool_input if isinstance(tool_input, Mapping) else None
    return block_tool_name(str(event_data.get("tool_name", "")), normalized_input)


def log_structured_block(
    logger: logging.Logger,
    *,
    session_id: str,
    event: str,
    tool: str,
    source: str,
    rule: str,
    reason: str,
) -> None:
    """Emit the canonical structured block log line used across transports."""
    logger.info(
        "BLOCK session=%s event=%s tool=%s source=%s rule=%s reason=%s",
        session_id,
        event,
        tool,
        source,
        rule,
        reason,
    )
