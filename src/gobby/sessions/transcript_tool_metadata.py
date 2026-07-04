from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.hooks.normalization import is_shell_tool, normalize_mcp_fields

_PROTOCOL_TOOL_NAME = "protocol_context"

TOOL_TYPE_MAP = {
    "bash": "bash",
    "shell": "bash",
    "read": "read",
    "read_file": "read",
    "write": "write",
    "edit": "edit",
    "multiedit": "edit",
    "apply_patch": "edit",
    "grep": "grep",
    "glob": "glob",
    "tool_search": "search",
    "tool_search_tool": "search",
    "websearch": "web_search",
    "webfetch": "web_fetch",
    "askuserquestion": "ask_user",
    "agent": "agent",
    "notebookedit": "notebook",
    "update_plan": "plan",
}


def _normalize_tool_data(
    tool_name: str,
    tool_input: Mapping[str, Any] | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        data["tool_input"] = dict(tool_input)
    return normalize_mcp_fields(data)


def classify_tool(
    tool_name: str | None,
    tool_input: Mapping[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Returns (tool_type, server_name). Extracts server from MCP tool metadata."""
    if not tool_name:
        return "unknown", None

    if tool_name.lower() == _PROTOCOL_TOOL_NAME:
        return "protocol", None

    tool_data = _normalize_tool_data(tool_name, tool_input)
    normalized_tool_name = str(tool_data.get("tool_name") or tool_name)
    lookup_tool_name = normalized_tool_name.casefold()
    mcp_server = tool_data.get("mcp_server")

    if isinstance(mcp_server, str) and mcp_server:
        return "mcp", mcp_server

    if normalized_tool_name in ("call_tool", "mcp__gobby__call_tool", "mcp_gobby_call_tool"):
        return "mcp", "unknown"

    if is_shell_tool(normalized_tool_name):
        return "bash", None

    if lookup_tool_name in TOOL_TYPE_MAP:
        return TOOL_TYPE_MAP[lookup_tool_name], None

    if normalized_tool_name.startswith("mcp__"):
        parts = normalized_tool_name.split("__")
        if len(parts) >= 3:
            return "mcp", parts[1]
        return "mcp", "unknown"

    return "unknown", None


def extract_result_metadata(
    tool_type: str, result_content: Any, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Extract tool-specific metadata from result for rich frontend rendering."""
    metadata: dict[str, Any] = {}
    if result_content is None:
        return metadata

    match tool_type:
        case "bash":
            if isinstance(result_content, dict):
                metadata["exit_code"] = result_content.get("exit_code")
                stdout = result_content.get("stdout", "")
                stderr = result_content.get("stderr", "")
                if isinstance(stdout, str):
                    metadata["stdout_lines"] = len(stdout.splitlines())
                if isinstance(stderr, str):
                    metadata["stderr_lines"] = len(stderr.splitlines())
        case "read":
            if isinstance(result_content, str):
                metadata["line_count"] = len(result_content.splitlines())
            if arguments:
                metadata["file_path"] = arguments.get("file_path") or arguments.get("path")
        case "edit":
            if arguments:
                metadata["file_path"] = arguments.get("file_path") or arguments.get("path")
        case "grep":
            if isinstance(result_content, dict):
                metadata["files_matched"] = result_content.get("files_matched")
                metadata["total_matches"] = result_content.get("total_matches")
        case "glob":
            if isinstance(result_content, list):
                metadata["files_found"] = len(result_content)

    return metadata
