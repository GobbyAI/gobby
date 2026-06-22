from __future__ import annotations

from typing import Any

from gobby.hooks.normalization import is_shell_tool

_PROTOCOL_TOOL_NAME = "protocol_context"

TOOL_TYPE_MAP = {
    "Bash": "bash",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "MultiEdit": "edit",
    "Grep": "grep",
    "Glob": "glob",
    "WebSearch": "web_search",
    "WebFetch": "web_fetch",
    "AskUserQuestion": "ask_user",
    "Agent": "agent",
    "NotebookEdit": "notebook",
}


def classify_tool(tool_name: str | None) -> tuple[str, str | None]:
    """Returns (tool_type, server_name). Extracts server from mcp__server__tool naming."""
    if not tool_name:
        return "unknown", None

    if tool_name.lower() == _PROTOCOL_TOOL_NAME:
        return "protocol", None

    if is_shell_tool(tool_name):
        return "bash", None

    if tool_name in TOOL_TYPE_MAP:
        return TOOL_TYPE_MAP[tool_name], None

    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
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
