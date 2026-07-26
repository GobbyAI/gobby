"""Hook event helpers for the tool proxy service."""

import asyncio
import inspect
import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from gobby.mcp_proxy.models import ToolProxyErrorCode

if TYPE_CHECKING:
    from gobby.hooks.events import HookEvent

logger = logging.getLogger("gobby.mcp.server")

_CODEX_RECONCILE_TIMEOUT_SECONDS = 60.0
_CODEX_RECONCILE_TASKS: dict[str, asyncio.Task[Any]] = {}

BeforeToolOutcome = Literal["policy_denied", "failed_pre_dispatch"]


async def _evaluate_workflow_handler(workflow_handler: Any, event: "HookEvent") -> Any:
    """Use the async workflow API when the caller already owns an event loop."""
    evaluate_async = getattr(workflow_handler, "evaluate_async", None)
    if inspect.iscoroutinefunction(evaluate_async):
        return await evaluate_async(event)

    from gobby.app_context import get_app_context

    app_context = get_app_context()
    if app_context is not None and app_context.db_executor is not None:
        return await app_context.run_db(workflow_handler.evaluate, event)
    return await asyncio.to_thread(workflow_handler.evaluate, event)


def _cleanup_codex_reconcile_task(session_id: str, task: asyncio.Task[Any]) -> None:
    if _CODEX_RECONCILE_TASKS.get(session_id) is task:
        _CODEX_RECONCILE_TASKS.pop(session_id, None)
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        logger.warning(
            "Codex transcript reconciliation failed",
            extra={"session_id": session_id},
            exc_info=(type(exception), exception, exception.__traceback__),
        )


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

    task = _CODEX_RECONCILE_TASKS.get(platform_session_id)
    if task is None or task.done():
        task = asyncio.create_task(reconcile(platform_session_id))
        _CODEX_RECONCILE_TASKS[platform_session_id] = task
        task.add_done_callback(
            lambda completed: _cleanup_codex_reconcile_task(platform_session_id, completed)
        )

    try:
        result = await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_CODEX_RECONCILE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "Timed out reconciling Codex transcript before task closure",
            extra={"session_id": platform_session_id},
        )
        return False
    except OSError:
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
) -> "HookEvent":
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
) -> Any | None:
    """Build the after_tool event when direct MCP execution owns completion."""
    from gobby.hooks.events import HookEvent, HookEventType
    from gobby.mcp_proxy.services.session_context import should_synthesize_direct_after_tool

    _hook_manager, _session_manager, _session, source, metadata, cwd, project_id = (
        service._resolve_tool_event_context(effective_session_id)
    )
    if not should_synthesize_direct_after_tool(source):
        return None
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
) -> tuple[
    str,
    str,
    dict[str, Any],
    dict[str, Any] | None,
    BeforeToolOutcome | None,
]:
    """Run workflow before_tool evaluation for direct MCP tool execution."""
    effective_session_id = await asyncio.to_thread(service._get_effective_session_id, session_id)
    if not effective_session_id:
        return server_name, tool_name, arguments, None, None

    hook_manager = service._resolve_hook_manager()
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
            "failed_pre_dispatch",
        )
    workflow_handler = getattr(hook_manager, "_workflow_handler", None) if hook_manager else None
    if workflow_handler is None:
        return server_name, tool_name, arguments, None, None

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
        response = await _evaluate_workflow_handler(workflow_handler, event)
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
            "failed_pre_dispatch",
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
            "policy_denied",
        )

    modified_input = response.modified_input
    if not isinstance(modified_input, dict):
        return server_name, tool_name, arguments, None, None

    updated_server_name = modified_input.get("server_name", server_name)
    updated_tool_name = modified_input.get("tool_name", tool_name)
    raw_arguments = modified_input.get("arguments", arguments)
    updated_arguments, error = service._prepare_arguments(raw_arguments)
    if error is not None:
        error["server_name"] = str(updated_server_name)
        error["tool_name"] = str(updated_tool_name)
        return server_name, tool_name, arguments, error, "failed_pre_dispatch"

    return str(updated_server_name), str(updated_tool_name), updated_arguments or {}, None, None


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
    if event is None:
        return
    try:
        response = await _evaluate_workflow_handler(workflow_handler, event)
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
