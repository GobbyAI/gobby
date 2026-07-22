"""Schema-guided invalid-argument responses for MCP tool calls."""

from __future__ import annotations

import logging
from typing import Any

from gobby.mcp_proxy.models import ToolProxyErrorCode

logger = logging.getLogger("gobby.mcp.server")

UNLOCKED_TOOLS_VARIABLE = "unlocked_tools"


def tool_schema_key(server_name: str, tool_name: str) -> str:
    """Return the session latch key for a target tool schema."""
    return f"{server_name}:{tool_name}"


def _session_variable_context(
    service: Any, session_id: str | None
) -> tuple[Any | None, str | None]:
    if not session_id:
        return None, None

    resolved_session_id = service._resolve_platform_session_id(session_id)
    if not resolved_session_id:
        return None, None

    hook_manager = service._resolve_hook_manager()
    db = getattr(hook_manager, "_database", None) if hook_manager else None
    if db is None:
        return None, resolved_session_id

    try:
        from gobby.workflows.state_manager import SessionVariableManager

        return SessionVariableManager(db), resolved_session_id
    except Exception as exc:
        logger.debug("Failed to initialize session variable manager: %s", exc)
        return None, resolved_session_id


def record_schema_shown(
    service: Any,
    session_id: str | None,
    *,
    server_name: str,
    tool_name: str,
) -> None:
    """Record that a target tool schema has been shown to the current session."""
    manager, resolved_session_id = _session_variable_context(service, session_id)
    if manager is None or resolved_session_id is None:
        return

    key = tool_schema_key(server_name, tool_name)
    try:
        manager.append_to_set_variable(resolved_session_id, UNLOCKED_TOOLS_VARIABLE, [key])
    except Exception as exc:
        logger.debug("Failed to record schema latch for %s: %s", resolved_session_id, exc)


def build_invalid_arguments_response(
    service: Any,
    *,
    server_name: str,
    tool_name: str,
    validation_errors: list[str],
    input_schema: dict[str, Any] | None = None,
    session_id: str | None = None,
    error_message: str | None = None,
    hint: str | None = "Review the schema for the accepted arguments.",
) -> dict[str, Any]:
    """Build an invalid-argument response with the current schema and lease."""
    errors = [str(error) for error in validation_errors if str(error)]
    if not errors and error_message:
        errors = [error_message]

    response: dict[str, Any] = {
        "success": False,
        "error": error_message or f"Invalid arguments: {errors}",
        "error_code": ToolProxyErrorCode.INVALID_ARGUMENTS.value,
        "validation_errors": errors,
        "server_name": server_name,
        "tool_name": tool_name,
    }
    if hint:
        response["hint"] = hint

    if input_schema is not None:
        response["schema"] = input_schema
        record_schema_shown(
            service,
            session_id,
            server_name=server_name,
            tool_name=tool_name,
        )

    return response
