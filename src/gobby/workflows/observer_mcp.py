"""MCP-call tracking observer for workflow session variables."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent

logger = logging.getLogger("gobby.workflows.observers")

_MAX_MCP_RESULT_ENTRIES = 64
_MAX_MCP_RESULT_FIELDS = 16
_MAX_MCP_RESULT_NAME_LENGTH = 128
_MAX_MCP_RESULT_STRING_LENGTH = 256
_MCP_FAILURE_FIELDS = ("success", "error", "status")


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
    if server_name == "gobby-skills" and inner_tool in {"get_skill", "get_skill_file"}:
        if tracked:
            _track_loaded_skill(
                variables,
                tool_output,
                session_id,
                reference=inner_tool == "get_skill_file",
            )
        elif inner_tool == "get_skill":
            _track_unresolvable_claimed_task_extra_skill(
                variables,
                event.data.get("tool_input") or {},
                tool_output,
                session_id,
            )


def _track_loaded_skill(
    variables: dict[str, Any],
    tool_output: dict[str, Any] | Any,
    session_id: str,
    *,
    reference: bool = False,
) -> None:
    """Record only completed instruction delivery in the appropriate context ledger."""
    name = (
        _extract_loaded_reference(tool_output)
        if reference
        else _extract_loaded_skill_name(tool_output)
    )
    if not name:
        return

    key = "loaded_skill_references" if reference else "loaded_skills"
    loaded = variables.setdefault(key, [])
    if not isinstance(loaded, list):
        loaded = [loaded] if loaded else []
    if name not in loaded:
        loaded.append(name)
    variables[key] = loaded
    logger.debug("Session %s: loaded skill tracked %s", session_id, name)


def _completed_instruction(tool_output: Any, field: str) -> dict[str, Any] | None:
    """Unwrap proxy results without accepting errors or incomplete pages."""
    candidate = tool_output
    for _ in range(4):
        if not isinstance(candidate, dict):
            return None
        if (
            candidate.get("error")
            or candidate.get("status") == "error"
            or candidate.get("success") is False
            or candidate.get("isError") is True
        ):
            return None
        instruction = candidate.get(field)
        if isinstance(instruction, dict):
            page = candidate.get("page")
            if not isinstance(page, dict) or page.get("complete") is not True:
                return None
            if page.get("next_cursor") is not None:
                return None
            if not isinstance(instruction.get("content"), str):
                return None
            return instruction
        candidate = candidate.get("result", candidate.get("structuredContent"))
    return None


def _extract_loaded_skill_name(tool_output: dict[str, Any] | Any) -> str | None:
    """Extract the resolved name from a completed get_skill result."""
    skill = _completed_instruction(tool_output, "skill")
    if skill is not None:
        name = skill.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _extract_loaded_reference(tool_output: Any) -> str | None:
    from gobby.skills.instruction_requirements import parse_instruction_requirement

    file = _completed_instruction(tool_output, "file")
    if file is None:
        return None
    name, path = file.get("skill_name"), file.get("path")
    if not isinstance(name, str) or not isinstance(path, str):
        return None
    try:
        return parse_instruction_requirement(f"{name}:{path}").identity
    except ValueError:
        return None


def _track_unresolvable_claimed_task_extra_skill(
    variables: dict[str, Any],
    tool_input: dict[str, Any] | Any,
    tool_output: dict[str, Any] | Any,
    session_id: str,
) -> None:
    """Record a claimed-task extra after get_skill definitively reports it missing."""
    name = _requested_skill_name(tool_input)
    extras = variables.get("claimed_task_extra_skills") or []
    if not name or not isinstance(extras, list) or name not in extras:
        return

    error = _skill_error(tool_output)
    if error != f"Skill not found: {name}":
        return

    unresolvable = variables.get("unresolvable_claimed_task_extra_skills") or []
    if not isinstance(unresolvable, list):
        unresolvable = []
    if name not in unresolvable:
        unresolvable.append(name)
        logger.warning(
            "Session %s: suppressing unresolvable claimed-task extra skill %s",
            session_id,
            name,
        )
    variables["unresolvable_claimed_task_extra_skills"] = unresolvable


def _requested_skill_name(tool_input: dict[str, Any] | Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    arguments = tool_input.get("arguments", tool_input.get("args", tool_input))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(arguments, dict):
        return None
    name = arguments.get("name")
    return name if isinstance(name, str) and name else None


def _skill_error(tool_output: dict[str, Any] | Any) -> str | None:
    candidate = tool_output
    for _ in range(3):
        if not isinstance(candidate, dict):
            return None
        error = candidate.get("error")
        if isinstance(error, str):
            return error
        candidate = candidate.get("result")
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

    if (
        len(server_name) > _MAX_MCP_RESULT_NAME_LENGTH
        or len(inner_tool) > _MAX_MCP_RESULT_NAME_LENGTH
    ):
        return True

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
    server_results[inner_tool] = _summarize_mcp_result(result)
    _trim_mcp_results(mcp_results)

    logger.debug(
        "Session %s: MCP call tracked %s/%s (result=%s)",
        session_id,
        server_name,
        inner_tool,
        "present" if result is not None else "null",
    )
    return True


def _summarize_mcp_result(result: Any) -> dict[str, Any] | None:
    """Keep only bounded top-level scalar fields needed by condition helpers."""
    if result is None:
        return None
    if not isinstance(result, dict):
        return {}

    summary: dict[str, Any] = {}
    ordered_fields = (
        *(field for field in _MCP_FAILURE_FIELDS if field in result),
        *(key for key in result if key not in _MCP_FAILURE_FIELDS),
    )
    for field in ordered_fields:
        if len(summary) >= _MAX_MCP_RESULT_FIELDS:
            break
        if not isinstance(field, str) or len(field) > _MAX_MCP_RESULT_NAME_LENGTH:
            continue
        value = result[field]
        if value is None or isinstance(value, bool | int | float):
            summary[field] = value
        elif isinstance(value, str) and len(value) <= _MAX_MCP_RESULT_STRING_LENGTH:
            summary[field] = value
        elif field == "error" and value:
            summary[field] = True
    return summary


def _trim_mcp_results(mcp_results: dict[str, Any]) -> None:
    """Evict oldest tool summaries until the total result count is bounded."""
    excess = sum(len(results) for results in mcp_results.values() if isinstance(results, dict))
    excess -= _MAX_MCP_RESULT_ENTRIES
    if excess <= 0:
        return

    for server_name in list(mcp_results):
        server_results = mcp_results[server_name]
        if not isinstance(server_results, dict):
            continue
        while server_results and excess > 0:
            del server_results[next(iter(server_results))]
            excess -= 1
        if not server_results:
            del mcp_results[server_name]
        if excess <= 0:
            return
