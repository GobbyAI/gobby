"""Codex item/* notification normalization.

Pure functions that turn raw Codex ThreadItem payloads from item/started and
item/completed notifications into normalized hook event data.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from gobby.hooks.normalization import normalize_tool_fields

logger = logging.getLogger(__name__)

TOOL_ITEM_TYPES: frozenset[str] = frozenset(
    {"commandExecution", "dynamicToolCall", "fileChange", "mcpToolCall"}
)

TOOLISH_FIELDS: frozenset[str] = frozenset(
    {
        "type",
        "itemType",
        "name",
        "tool",
        "toolName",
        "tool_name",
        "arguments",
        "toolArgs",
        "tool_input",
        "input",
        "output",
        "contentItems",
        "result",
        "toolResult",
        "callId",
        "call_id",
        "toolUseId",
        "tool_use_id",
    }
)

_FUNCTIONS_EXEC_NAMES = frozenset({"exec", "functions.exec"})
_EXEC_COMMAND_CALL_RE = re.compile(r"\btools\.exec_command\s*\(")
_EXEC_COMMAND_LITERAL_RE = re.compile(
    r'(?:^|[{,])\s*cmd\s*:\s*("(?:\\.|[^"\\])*")',
    re.DOTALL,
)
_YIELDED_CELL_RE = re.compile(r"^Script running with cell ID ([A-Za-z0-9._:-]+)\s*$")


def compose_mcp_tool_name(server: str, tool: str) -> str:
    """Return canonical MCP tool-name form used across adapters."""
    return f"mcp__{server}__{tool}"


def _dynamic_tool_name(item_data: dict[str, Any]) -> str:
    tool = item_data.get("tool") or item_data.get("name") or item_data.get("toolName")
    if not isinstance(tool, str) or not tool:
        return ""
    namespace = item_data.get("namespace")
    if isinstance(namespace, str) and namespace and "." not in tool:
        return f"{namespace}.{tool}"
    return tool


def extract_functions_exec_command(arguments: Any) -> str | None:
    """Extract one literal nested ``exec_command`` command, failing closed."""
    if isinstance(arguments, dict):
        command = arguments.get("cmd")
        return command if isinstance(command, str) and command else None
    if not isinstance(arguments, str):
        return None
    if len(_EXEC_COMMAND_CALL_RE.findall(arguments)) != 1:
        return None
    matches = _EXEC_COMMAND_LITERAL_RE.findall(arguments)
    if len(matches) != 1:
        return None
    try:
        command = json.loads(matches[0])
    except (TypeError, ValueError):
        return None
    return command if isinstance(command, str) and command else None


def _iter_wrapper_output_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    result: list[str] = []
    if isinstance(value, list):
        for block in value:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                result.append(block["text"])
            elif isinstance(block, (dict, list, str)):
                result.extend(_iter_wrapper_output_text(block))
        return result
    if isinstance(value, dict):
        for key in ("content", "output"):
            nested = value.get(key)
            if isinstance(nested, (dict, list, str)):
                result.extend(_iter_wrapper_output_text(nested))
    return result


def extract_yielded_cell_id(data: dict[str, Any]) -> str | None:
    """Read the functions wrapper's correlation token without inferring outcome."""
    output = data.get("tool_output")
    for text in _iter_wrapper_output_text(output):
        for line in text.splitlines():
            first_nonblank = line.strip()
            if not first_nonblank:
                continue
            match = _YIELDED_CELL_RE.fullmatch(first_nonblank)
            return match.group(1) if match else None
    return None


def extract_wait_cell_id(data: dict[str, Any]) -> str | None:
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    cell_id = tool_input.get("cell_id")
    if isinstance(cell_id, str) and cell_id:
        return cell_id
    if isinstance(cell_id, int) and not isinstance(cell_id, bool):
        return str(cell_id)
    return None


class DynamicExecCorrelator:
    """Correlate yielded ``functions.exec`` calls with their final wait item."""

    def __init__(self, max_pending: int = 64) -> None:
        self._max_pending = max_pending
        self._pending: dict[str, dict[str, Any]] = {}

    def correlate(self, data: dict[str, Any]) -> dict[str, Any]:
        """Attach the original shell command to a correlated final wait result."""
        original_tool = data.get("_original_tool_name") or data.get("tool_name")
        yielded_cell_id = extract_yielded_cell_id(data)

        if original_tool in _FUNCTIONS_EXEC_NAMES and yielded_cell_id is not None:
            tool_input = data.get("tool_input")
            if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
                if len(self._pending) >= self._max_pending:
                    self._pending.pop(next(iter(self._pending)))
                self._pending[yielded_cell_id] = dict(tool_input)
            return data

        if original_tool not in {"wait", "functions.wait"}:
            return data

        cell_id = extract_wait_cell_id(data)
        if cell_id is None or yielded_cell_id is not None:
            return data
        pending_input = self._pending.pop(cell_id, None)
        if pending_input is None:
            return data

        data["_original_tool_name"] = str(original_tool)
        data["tool_name"] = "Bash"
        data["tool_input"] = pending_input
        return data


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
    toolish_fields = TOOLISH_FIELDS - {"type", "itemType"}
    return any(field in item for field in toolish_fields)


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
    if not raw_tool_name and item_type == "dynamicToolCall":
        raw_tool_name = _dynamic_tool_name(item_data)
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

    if item_type == "commandExecution":
        item_data["tool_response"] = {
            "output": item_data.get("aggregatedOutput"),
            "exitCode": item_data.get("exitCode"),
            "status": item_data.get("status"),
        }
    elif item_type == "dynamicToolCall":
        dynamic_name = _dynamic_tool_name(item_data)
        item_data["tool_response"] = {
            "content": item_data.get("contentItems"),
            "success": item_data.get("success"),
            "status": item_data.get("status"),
        }
        if dynamic_name in _FUNCTIONS_EXEC_NAMES:
            command = extract_functions_exec_command(item_data.get("arguments"))
            if command is not None:
                item_data["_original_tool_name"] = dynamic_name
                item_data["tool_name"] = "Bash"
                item_data["tool_input"] = {"command": command}

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
        except (TypeError, ValueError) as exc:
            logger.debug("Failed to parse MCP arguments JSON %r: %s", raw, exc)
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}
