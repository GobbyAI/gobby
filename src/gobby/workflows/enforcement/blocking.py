"""Tool blocking helpers for workflow engine.

Provides discovery-tool checks and schema-unlock tracking used by the
rule engine's blocking conditions.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# MCP discovery tools that don't require prior schema lookup
DISCOVERY_TOOLS = {
    "list_mcp_servers",
    "list_tools",
    "get_tool_schema",
    "search_tools",
    "recommend_tools",
    "list_skills",
    "get_skill",
    "search_skills",
}


INFRASTRUCTURE_TOOLS = {
    "set_variable",
    "get_variable",
}


MESSAGE_DELIVERY_TOOLS = {"deliver_pending_messages"}


def is_message_delivery_tool(tool_name: str | None) -> bool:
    """Check if the tool is a message delivery tool.

    These tools are excluded from the notify-unread-mail context injection
    so agents aren't nudged while already reading their mail.

    Args:
        tool_name: The MCP tool name (from event.data.mcp_tool)

    Returns:
        True if this is a message delivery tool
    """
    return tool_name in MESSAGE_DELIVERY_TOOLS if tool_name else False


def is_infrastructure_tool(tool_name: str | None) -> bool:
    """Check if the tool is an infrastructure tool that should always be allowed.

    These tools manage session state and are required for agents to satisfy
    gate conditions (e.g., stop gates that require set_variable calls).

    Args:
        tool_name: The MCP tool name (from tool_input.tool_name)

    Returns:
        True if this is an infrastructure tool
    """
    return tool_name in INFRASTRUCTURE_TOOLS if tool_name else False


def is_discovery_tool(tool_name: str | None) -> bool:
    """Check if the tool is a discovery/introspection tool.

    These tools are allowed without prior schema lookup since they ARE
    the discovery mechanism.

    Args:
        tool_name: The MCP tool name (from tool_input.tool_name)

    Returns:
        True if this is a discovery tool that doesn't need schema unlock
    """
    return tool_name in DISCOVERY_TOOLS if tool_name else False


def is_tool_unlocked(
    tool_input: dict[str, Any],
    variables: dict[str, Any],
) -> bool:
    """Check if a tool has been unlocked via prior get_tool_schema call.

    Args:
        tool_input: The tool input containing server_name and tool_name
        variables: Workflow state variables containing unlocked_tools list

    Returns:
        True if the server:tool combo was previously unlocked via get_tool_schema
    """
    # Support 'server' alias for 'server_name' and 'tool' alias for 'tool_name'
    server = tool_input.get("server_name") or tool_input.get("server") or ""
    tool = tool_input.get("tool_name") or tool_input.get("tool") or ""

    if not server or not tool:
        # Don't log here as it might be called speculatively
        return False

    key = f"{server}:{tool}"
    unlocked = variables.get("unlocked_tools", [])

    is_unlocked = key in unlocked
    if not is_unlocked:
        logger.debug(f"is_tool_unlocked check failed for {key}. Unlocked tools: {unlocked}")

    return is_unlocked


def is_server_listed(
    tool_input: dict[str, Any],
    variables: dict[str, Any],
) -> bool:
    """Check if list_tools has been called for this server.

    Args:
        tool_input: The tool input containing server_name or server
        variables: Workflow state variables containing listed_servers list

    Returns:
        True if the server was previously listed via list_tools
    """
    server = tool_input.get("server_name") or tool_input.get("server") or ""
    if not server:
        return False
    return server in variables.get("listed_servers", [])


# CLI config directories whose .md files are exempt from task-before-edit
# enforcement (plan files, notes, specs).  Any .md file under these dirs
# qualifies — no "/plans/" subdirectory requirement.
_CLI_DIR_SEGMENTS = (
    f"{os.sep}.gobby{os.sep}",
    f"{os.sep}.claude{os.sep}",
    f"{os.sep}.gemini{os.sep}",
    f"{os.sep}.codex{os.sep}",
)


def is_plan_file(file_path: str, source: str | None = None) -> bool:
    """Check if a file is a plan file that may be edited without a task.

    Any ``.md`` file under a recognised CLI config directory is treated as
    a plan file.  Recognised directories: ``.gobby/``, ``.claude/``,
    ``.gemini/``, ``.codex/``.

    Args:
        file_path: Absolute or relative path to the file being edited.
        source: Adapter source (e.g. ``"claude_code"``).  Currently unused
            but accepted for forward-compatibility with the rule signature.

    Returns:
        True if *file_path* is a recognised plan file.
    """
    if not file_path:
        return False

    # Normalise so segment matching works on any platform
    normalised = os.path.normpath(file_path)

    if not normalised.endswith(".md"):
        return False

    return any(seg in normalised for seg in _CLI_DIR_SEGMENTS)


def _extract_change_path(change: Any) -> str | None:
    """Extract a touched file path from a file-change dict."""
    if not isinstance(change, dict):
        return None

    for key in (
        "file_path",
        "path",
        "new_path",
        "newPath",
        "target_path",
        "targetPath",
    ):
        value = change.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _dedupe_paths(paths: list[str]) -> list[str]:
    """Dedupe paths while preserving order."""
    unique_paths: list[str] = []
    for path in paths:
        if path and path not in unique_paths:
            unique_paths.append(path)
    return unique_paths


def get_touched_file_paths(tool_input: Any) -> list[str]:
    """Return all file paths touched by a write-like tool input."""
    if isinstance(tool_input, dict):
        file_paths = tool_input.get("file_paths")
        if isinstance(file_paths, list):
            normalized = [
                path.strip()
                for path in file_paths
                if isinstance(path, str) and path.strip()
            ]
            if normalized:
                return _dedupe_paths(normalized)

        file_path = tool_input.get("file_path")
        if isinstance(file_path, str) and file_path.strip():
            return [file_path.strip()]

        changes = tool_input.get("changes")
        if isinstance(changes, list):
            return _dedupe_paths(
                [path for change in changes if (path := _extract_change_path(change))]
            )

        return []

    if isinstance(tool_input, list):
        return _dedupe_paths([path for change in tool_input if (path := _extract_change_path(change))])

    return []


def requires_task_for_any_touched_file(
    tool_input: Any,
    source: str | None = None,
    plan_mode: bool = False,
) -> bool:
    """Return True when any touched file should be task-gated.

    The helper fails closed: when no touched paths can be determined for a
    write-like tool, the edit is treated as requiring a task.
    """
    touched_paths = get_touched_file_paths(tool_input)
    if not touched_paths:
        return True

    for path in touched_paths:
        if is_plan_file(path, source):
            continue
        if plan_mode and path.endswith(".md"):
            continue
        return True

    return False
