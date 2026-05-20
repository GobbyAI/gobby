"""Hook event helpers for the tool proxy service."""

import asyncio
import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from gobby.mcp_proxy.models import ToolProxyErrorCode

logger = logging.getLogger("gobby.mcp.server")


def build_before_tool_event(
    service: Any,
    effective_session_id: str,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    """Build the before_tool event used for direct MCP execution."""
    from gobby.hooks.events import HookEvent, HookEventType

    _hook_manager, _session_manager, _session, source, metadata, cwd, project_id = (
        service._resolve_tool_event_context(effective_session_id)
    )

    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=effective_session_id,
        source=source,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": deepcopy(arguments),
            },
        },
        metadata=metadata,
        cwd=cwd,
        project_id=project_id,
    )


async def apply_before_tool_enforcement(
    service: Any,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    session_id: str | None,
) -> tuple[str, str, dict[str, Any], dict[str, Any] | None]:
    """Run workflow before_tool evaluation for direct MCP tool execution."""
    effective_session_id = service._get_effective_session_id(session_id)
    if not effective_session_id:
        return server_name, tool_name, arguments, None

    hook_manager = service._resolve_hook_manager()
    workflow_handler = getattr(hook_manager, "_workflow_handler", None) if hook_manager else None
    if workflow_handler is None:
        return server_name, tool_name, arguments, None

    event = service._build_before_tool_event(
        effective_session_id=effective_session_id,
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
    )
    has_pending_context = getattr(workflow_handler, "has_pending_tool_context", None)
    if callable(has_pending_context):
        try:
            if has_pending_context(event.source, effective_session_id, event.data):
                event.metadata["_mcp_proxy_duplicate_before_tool"] = True
        except Exception as exc:
            logger.debug(
                "Failed to check pending tool context for %s/%s: %s",
                server_name,
                tool_name,
                exc,
                exc_info=True,
            )
    try:
        from gobby.app_context import get_app_context

        app_context = get_app_context()
        if app_context is not None and app_context.db_executor is not None:
            response = await app_context.run_db(workflow_handler.evaluate, event)
        else:
            response = await asyncio.to_thread(workflow_handler.evaluate, event)
    except Exception as exc:
        logger.warning(
            "Workflow evaluation failed for %s/%s: %s",
            server_name,
            tool_name,
            exc,
            exc_info=True,
        )
        return (
            server_name,
            tool_name,
            arguments,
            {
                "success": False,
                "error": f"Workflow evaluation failed: {exc}",
                "error_code": ToolProxyErrorCode.TOOL_BLOCKED.value,
                "server_name": server_name,
                "tool_name": tool_name,
            },
        )

    if response.decision != "allow":
        return (
            server_name,
            tool_name,
            arguments,
            {
                "success": False,
                "error": response.reason or "Tool call blocked by workflow rules.",
                "error_code": ToolProxyErrorCode.TOOL_BLOCKED.value,
                "server_name": server_name,
                "tool_name": tool_name,
            },
        )

    modified_input = response.modified_input
    if not isinstance(modified_input, dict):
        return server_name, tool_name, arguments, None

    updated_server_name = modified_input.get("server_name", server_name)
    updated_tool_name = modified_input.get("tool_name", tool_name)
    raw_arguments = modified_input.get("arguments", arguments)
    updated_arguments, error = service._prepare_arguments(raw_arguments)
    if error is not None:
        error["server_name"] = str(updated_server_name)
        error["tool_name"] = str(updated_tool_name)
        return server_name, tool_name, arguments, error

    return str(updated_server_name), str(updated_tool_name), updated_arguments or {}, None
