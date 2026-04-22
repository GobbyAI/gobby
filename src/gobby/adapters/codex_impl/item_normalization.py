"""Codex item/* notification normalization.

Pure functions that turn raw Codex ThreadItem payloads from item/started and
item/completed notifications into normalized hook event data.
"""

from __future__ import annotations

import json
from typing import Any

from gobby.hooks.normalization import normalize_tool_fields

TOOL_ITEM_TYPES: frozenset[str] = frozenset({"commandExecution", "fileChange", "mcpToolCall"})

TOOLISH_FIELDS: frozenset[str] = frozenset(
    {
        "type",
        "itemType",
        "name",
        "toolName",
        "tool_name",
        "arguments",
        "toolArgs",
        "tool_input",
        "input",
        "output",
        "result",
        "toolResult",
        "callId",
        "call_id",
        "toolUseId",
        "tool_use_id",
    }
)


def compose_mcp_tool_name(server: str, tool: str) -> str:
    """Return canonical MCP tool-name form used across adapters."""
    return f"mcp__{server}__{tool}"


def extract_completed_item_payload(params: dict[str, Any]) -> dict[str, Any]:
    """Return tool item payload from item/started or item/completed params."""
    item = params.get("item")
    if isinstance(item, dict):
        return item
    if any(field in params for field in TOOLISH_FIELDS):
        return params
    return {}


def looks_like_tool_item(item: dict[str, Any]) -> bool:
    """Identify Codex items that represent tool execution."""
    item_type = item.get("type") or item.get("itemType")
    if item_type in TOOL_ITEM_TYPES:
        return True
    if any(isinstance(item.get(tool_type), dict) for tool_type in TOOL_ITEM_TYPES):
        return True
    return any(field in item for field in TOOLISH_FIELDS)


def build_tool_event_data(
    item: dict[str, Any],
    *,
    tool_name_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Normalize a Codex tool item into hook event data."""
    item_type = item.get("type") or item.get("itemType") or ""
    nested_payload = item.get(item_type)

    item_data: dict[str, Any] = {}
    if isinstance(nested_payload, dict):
        item_data.update(nested_payload)
    item_data.update(item)

    item_id = item_data.get("id") or item_data.get("itemId") or ""
    raw_tool_name = item_data.get("tool_name") or item_data.get("toolName") or item_data.get("name")
    if not raw_tool_name and item_type == "mcpToolCall":
        server = item_data.get("server") or item_data.get("serverName")
        mcp_tool = item_data.get("tool") or item_data.get("toolName") or item_data.get("name")
        if isinstance(server, str) and server and isinstance(mcp_tool, str) and mcp_tool:
            raw_tool_name = compose_mcp_tool_name(server, mcp_tool)

    if isinstance(raw_tool_name, str) and raw_tool_name:
        mapped = tool_name_map.get(raw_tool_name, raw_tool_name) if tool_name_map else raw_tool_name
        item_data.setdefault("tool_name", mapped)
    elif item_type == "commandExecution":
        item_data.setdefault("tool_name", "Bash")
    elif item_type == "fileChange":
        item_data.setdefault("tool_name", "Write")

    if "tool_input" not in item_data:
        if "arguments" in item_data and "toolArgs" not in item_data:
            item_data["toolArgs"] = item_data["arguments"]
        elif "input" in item_data:
            item_data["tool_input"] = item_data["input"]

    if "tool_response" not in item_data and "tool_result" not in item_data:
        if "output" in item_data:
            item_data["tool_response"] = item_data["output"]
        elif "result" in item_data:
            item_data["tool_response"] = item_data["result"]

    item_data.setdefault("item_id", item_id)
    item_data.setdefault("item_type", item_type)
    item_data.setdefault("status", item.get("status", item_data.get("status", "")))

    normalize_tool_fields(item_data)
    return item_data


def build_pre_tool_lifecycle_payload(
    params: dict[str, Any],
    *,
    tool_name_map: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Extract tool name and input from an item/started notification."""
    item = extract_completed_item_payload(params)
    if not item or not looks_like_tool_item(item):
        return None

    item_type = item.get("type") or item.get("itemType") or ""
    if item_type == "contextCompaction":
        return None

    data = build_tool_event_data(item, tool_name_map=tool_name_map)
    tool_name = data.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    return tool_name, tool_input


def build_post_tool_lifecycle_payload(
    params: dict[str, Any],
    *,
    tool_name_map: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any], Any] | None:
    """Extract tool name, input, and response from item/completed params."""
    item = extract_completed_item_payload(params)
    if not item or not looks_like_tool_item(item):
        return None

    item_type = item.get("type") or item.get("itemType") or ""
    if item_type == "contextCompaction":
        return None

    data = build_tool_event_data(item, tool_name_map=tool_name_map)
    tool_name = data.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    return tool_name, tool_input, data.get("tool_response")


def parse_mcp_arguments(raw: Any) -> dict[str, Any]:
    """Parse MCP arguments from a dict or JSON string into a dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}
