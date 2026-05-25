"""MCP-call tracking observer for workflow session variables."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.workflows.observer_utils import _json_safe

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent

logger = logging.getLogger("gobby.workflows.observers")


def detect_mcp_call(event: HookEvent, variables: dict[str, Any], session_id: str) -> None:
    """Track MCP tool calls by server/tool for rule engine conditions."""
    if not event.data:
        return

    server_name = event.data.get("mcp_server", "")
    inner_tool = event.data.get("mcp_tool", "")

    if not server_name or not inner_tool:
        return

    tool_output = event.data.get("tool_output") or {}

    tracked = _track_mcp_call(variables, server_name, inner_tool, tool_output, session_id)
    if tracked and server_name == "gobby-skills" and inner_tool == "get_skill":
        _track_loaded_skill(variables, tool_output, session_id)


def _track_loaded_skill(
    variables: dict[str, Any],
    tool_output: dict[str, Any] | Any,
    session_id: str,
) -> None:
    """Record a successful agent-visible gobby-skills:get_skill result."""
    name = _extract_loaded_skill_name(tool_output)
    if not name:
        return

    loaded = variables.setdefault("loaded_skills", [])
    if not isinstance(loaded, list):
        loaded = [loaded] if loaded else []
    if name not in loaded:
        loaded.append(name)
    variables["loaded_skills"] = loaded
    logger.debug("Session %s: loaded skill tracked %s", session_id, name)


def _extract_loaded_skill_name(tool_output: dict[str, Any] | Any) -> str | None:
    """Extract the resolved skill name from a successful get_skill tool result."""
    if not isinstance(tool_output, dict):
        return None
    if tool_output.get("error") or tool_output.get("status") == "error":
        return None
    if tool_output.get("success") is False:
        return None

    candidates = [tool_output]
    result = tool_output.get("result")
    if isinstance(result, dict):
        if result.get("success") is False or result.get("error"):
            return None
        candidates.append(result)
        nested_result = result.get("result")
        if isinstance(nested_result, dict):
            candidates.append(nested_result)

    for candidate in candidates:
        skill = candidate.get("skill") if isinstance(candidate, dict) else None
        if isinstance(skill, dict):
            name = skill.get("name")
            if isinstance(name, str) and name:
                return name
    return None


def _track_mcp_call(
    variables: dict[str, Any],
    server_name: str,
    inner_tool: str,
    tool_output: dict[str, Any] | Any,
    session_id: str,
) -> bool:
    """Track a successful MCP call in session variables.

    Returns True if call succeeded and was tracked, False if it failed.
    """
    result = None
    is_error = False
    if isinstance(tool_output, dict):
        if (
            tool_output.get("error")
            or tool_output.get("status") == "error"
            or tool_output.get("success") is False
        ):
            is_error = True
        else:
            result = tool_output.get("result")
            if isinstance(result, dict) and (result.get("error") or result.get("success") is False):
                is_error = True

    if is_error:
        return False

    mcp_calls_value = variables.get("mcp_calls")
    if not isinstance(mcp_calls_value, dict):
        mcp_calls: dict[str, Any] = {}
        variables["mcp_calls"] = mcp_calls
    else:
        mcp_calls = mcp_calls_value

    server_calls_value = mcp_calls.get(server_name)
    if not isinstance(server_calls_value, list):
        server_calls: list[Any] = []
        mcp_calls[server_name] = server_calls
    else:
        server_calls = server_calls_value
    if inner_tool not in server_calls:
        server_calls.append(inner_tool)

    mcp_results_value = variables.get("mcp_results")
    if not isinstance(mcp_results_value, dict):
        mcp_results: dict[str, Any] = {}
        variables["mcp_results"] = mcp_results
    else:
        mcp_results = mcp_results_value

    server_results_value = mcp_results.get(server_name)
    if not isinstance(server_results_value, dict):
        server_results: dict[str, Any] = {}
        mcp_results[server_name] = server_results
    else:
        server_results = server_results_value
    server_results[inner_tool] = _json_safe(result)

    logger.debug(
        "Session %s: MCP call tracked %s/%s (result=%s)",
        session_id,
        server_name,
        inner_tool,
        "present" if result is not None else "null",
    )
    return True
