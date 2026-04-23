"""Synthetic hook event helpers for the tool proxy service."""

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
    """Build the synthetic before_tool event used for direct MCP execution."""
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


def build_synthetic_tool_output(result: Any) -> dict[str, Any]:
    """Wrap direct MCP call results in the observer-friendly AFTER_TOOL shape."""
    try:
        copied_result = deepcopy(result)
    except Exception:
        copied_result = result

    wrapped = {"result": copied_result}
    if isinstance(result, dict):
        if result.get("success") is False:
            wrapped["success"] = False
        if result.get("status") == "error":
            wrapped["status"] = "error"
        error_msg = result.get("error")
        if error_msg:
            wrapped["error"] = error_msg
    return wrapped


def should_emit_synthetic_after_tool(
    *,
    session: Any | None,
    source: Any,
    enforce_workflow: bool,
) -> bool:
    """Return True when the Codex-terminal MCP compatibility shim should run."""
    if not enforce_workflow or session is None:
        return False

    if getattr(source, "value", source) != "codex":
        return False

    return getattr(session, "session_type", "terminal") == "terminal"


def build_after_tool_event(
    service: Any,
    effective_session_id: str,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    *,
    is_failure: bool,
) -> Any | None:
    """Build the synthetic AFTER_TOOL compatibility event for Codex terminal MCP calls."""
    from gobby.hooks.events import HookEvent, HookEventType

    hook_manager, _session_manager, session, source, metadata, cwd, project_id = (
        service._resolve_tool_event_context(effective_session_id)
    )
    if hook_manager is None or not service._should_emit_synthetic_after_tool(
        session=session,
        source=source,
        enforce_workflow=True,
    ):
        return None

    external_id = metadata.get("external_id")
    if not isinstance(external_id, str) or not external_id:
        return None

    event_metadata = dict(metadata)
    event_metadata["_synthetic_codex_mcp_after_tool"] = True
    if is_failure:
        event_metadata["is_failure"] = True

    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": server_name,
            "tool_name": tool_name,
            "arguments": deepcopy(arguments),
        },
        "tool_output": service._build_synthetic_tool_output(result),
        "mcp_server": server_name,
        "mcp_tool": tool_name,
    }
    if is_failure:
        data["is_error"] = True

    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=external_id,
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        metadata=event_metadata,
        cwd=cwd,
        project_id=project_id,
    )


async def emit_synthetic_after_tool(
    service: Any,
    *,
    effective_session_id: str | None,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    result: Any,
    enforce_workflow: bool,
    is_failure: bool,
) -> None:
    """Emit the internal Codex-terminal MCP AFTER_TOOL compatibility event."""
    if not effective_session_id or not enforce_workflow:
        return

    event = service._build_after_tool_event(
        effective_session_id=effective_session_id,
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
        result=result,
        is_failure=is_failure,
    )
    if event is None:
        return

    hook_manager = service._resolve_hook_manager()
    if hook_manager is None:
        return

    try:
        await asyncio.to_thread(hook_manager.handle, event)
    except Exception as exc:
        logger.warning(
            "Synthetic Codex MCP AFTER_TOOL compatibility event failed for %s/%s: %s",
            server_name,
            tool_name,
            exc,
            exc_info=True,
        )


def build_proxy_tool_after_tool_event(
    service: Any,
    effective_session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    result: Any,
    *,
    is_failure: bool,
) -> Any | None:
    """Build a synthetic AFTER_TOOL event for daemon-owned proxy tools."""
    from gobby.hooks.events import HookEvent, HookEventType

    hook_manager, _session_manager, session, source, metadata, cwd, project_id = (
        service._resolve_tool_event_context(effective_session_id)
    )
    if hook_manager is None or not service._should_emit_synthetic_after_tool(
        session=session,
        source=source,
        enforce_workflow=True,
    ):
        return None

    external_id = metadata.get("external_id")
    if not isinstance(external_id, str) or not external_id:
        return None

    event_metadata = dict(metadata)
    event_metadata["_synthetic_codex_mcp_after_tool"] = True
    if is_failure:
        event_metadata["is_failure"] = True

    data: dict[str, Any] = {
        "tool_name": f"mcp__gobby__{tool_name}",
        "tool_input": deepcopy(tool_input),
        "tool_output": service._build_synthetic_tool_output(result),
        "mcp_server": "gobby",
        "mcp_tool": tool_name,
    }
    if is_failure:
        data["is_error"] = True

    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=external_id,
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        metadata=event_metadata,
        cwd=cwd,
        project_id=project_id,
    )


async def emit_synthetic_proxy_after_tool(
    service: Any,
    *,
    session_id: str | None,
    tool_name: str,
    tool_input: dict[str, Any],
    result: Any,
    is_failure: bool = False,
) -> None:
    """Emit the internal Codex-terminal AFTER_TOOL shim for proxy discovery tools."""
    effective_session_id = service._get_effective_session_id(session_id)
    if not effective_session_id:
        return

    event = service._build_proxy_tool_after_tool_event(
        effective_session_id=effective_session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        result=result,
        is_failure=is_failure,
    )
    if event is None:
        return

    hook_manager = service._resolve_hook_manager()
    if hook_manager is None:
        return

    try:
        await asyncio.to_thread(hook_manager.handle, event)
    except Exception as exc:
        logger.warning(
            "Synthetic Codex MCP AFTER_TOOL compatibility event failed for gobby/%s: %s",
            tool_name,
            exc,
            exc_info=True,
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
    try:
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
