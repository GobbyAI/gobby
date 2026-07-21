"""Hook event helpers for the tool proxy service."""

import asyncio
import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from gobby.mcp_proxy.models import ToolProxyErrorCode

logger = logging.getLogger("gobby.mcp.server")

_CODEX_RECONCILE_TIMEOUT_SECONDS = 2.0


async def _reconcile_codex_close_transcript(
    hook_manager: Any,
    event: Any,
    *,
    server_name: str,
    tool_name: str,
    effective_session_id: str,
) -> bool:
    """Bound terminal Codex catch-up so close rules see the latest shell result."""
    source = getattr(event.source, "value", event.source)
    if source != "codex" or server_name != "gobby-tasks" or tool_name != "close_task":
        return True

    processor = getattr(hook_manager, "_message_processor", None)
    reconcile = getattr(processor, "reconcile_codex_transcript", None)
    if not callable(reconcile):
        return False
    platform_session_id = event.metadata.get("_platform_session_id")
    if not isinstance(platform_session_id, str) or not platform_session_id:
        platform_session_id = effective_session_id

    try:
        result = await asyncio.wait_for(
            reconcile(platform_session_id),
            timeout=_CODEX_RECONCILE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Timed out reconciling Codex transcript before task closure",
            extra={"session_id": platform_session_id},
        )
        return False
    except Exception:
        logger.warning(
            "Failed to reconcile Codex transcript before task closure",
            extra={"session_id": platform_session_id},
            exc_info=True,
        )
        return False

    if not getattr(result, "flushed", False):
        logger.debug(
            "Codex transcript reconciliation did not flush before task closure",
            extra={
                "session_id": platform_session_id,
                "error": getattr(result, "error", None),
            },
        )
        return False
    return True


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


def build_after_tool_event(
    service: Any,
    effective_session_id: str,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    tool_output: Any,
) -> Any:
    """Build the after_tool event used for direct MCP execution."""
    from gobby.hooks.events import HookEvent, HookEventType

    _hook_manager, _session_manager, _session, source, metadata, cwd, project_id = (
        service._resolve_tool_event_context(effective_session_id)
    )
    metadata["_mcp_proxy_direct_after_tool"] = True

    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
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
            "tool_output": deepcopy(tool_output),
            "mcp_server": server_name,
            "mcp_tool": tool_name,
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
    effective_session_id = await asyncio.to_thread(service._get_effective_session_id, session_id)
    if not effective_session_id:
        return server_name, tool_name, arguments, None

    hook_manager = service._resolve_hook_manager()
    workflow_handler = getattr(hook_manager, "_workflow_handler", None) if hook_manager else None
    if workflow_handler is None:
        return server_name, tool_name, arguments, None

    event = await asyncio.to_thread(
        service._build_before_tool_event,
        effective_session_id=effective_session_id,
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
    )
    event_handlers = getattr(hook_manager, "event_handlers", None)
    if event_handlers is not None:
        from gobby.hooks.session_activation import reconcile_session_activation

        activation_result = await asyncio.to_thread(
            reconcile_session_activation,
            event,
            event_handlers,
            logger=logger,
        )
        logger.debug("Session activation reconciliation result: %s", activation_result)
    reconciled = await _reconcile_codex_close_transcript(
        hook_manager,
        event,
        server_name=server_name,
        tool_name=tool_name,
        effective_session_id=effective_session_id,
    )
    if not reconciled:
        return (
            server_name,
            tool_name,
            arguments,
            {
                "success": False,
                "error": "Codex transcript reconciliation did not complete; retry task closure.",
                "error_code": ToolProxyErrorCode.TOOL_BLOCKED.value,
                "server_name": server_name,
                "tool_name": tool_name,
                "retryable": True,
            },
        )
    has_pending_context = getattr(workflow_handler, "has_pending_tool_context", None)
    if callable(has_pending_context):
        try:
            if await asyncio.to_thread(
                has_pending_context, event.source, effective_session_id, event.data
            ):
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


async def apply_after_tool_workflow(
    service: Any,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    session_id: str | None,
    tool_output: Any,
) -> None:
    """Run workflow after_tool processing for direct MCP tool execution."""
    effective_session_id = await asyncio.to_thread(service._get_effective_session_id, session_id)
    if not effective_session_id:
        return

    hook_manager = service._resolve_hook_manager()
    workflow_handler = getattr(hook_manager, "_workflow_handler", None) if hook_manager else None
    if workflow_handler is None:
        return

    event = await asyncio.to_thread(
        build_after_tool_event,
        service=service,
        effective_session_id=effective_session_id,
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
        tool_output=tool_output,
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
            "Workflow after_tool evaluation failed for %s/%s: %s",
            server_name,
            tool_name,
            exc,
            exc_info=True,
        )
        return

    if response.decision != "allow":
        logger.debug(
            "Workflow after_tool response for %s/%s returned %s: %s",
            server_name,
            tool_name,
            response.decision,
            response.reason,
        )
