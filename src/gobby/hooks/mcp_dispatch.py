"""Async MCP call dispatch for rule engine effects.

Provides ``dispatch_mcp_calls`` which executes ``mcp_call`` effects emitted
by the rule engine.  This is the async-native equivalent of
``HookManager._dispatch_mcp_calls`` (which runs in the sync hook-manager
context and needs thread-safe loop scheduling).

Used by the web-chat path (``ChatMixin._fire_lifecycle``) where we already
have a running asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextvars import Token
from typing import Any

from gobby.hooks.background_tasks import create_background_task
from gobby.hooks.events import HookEvent
from gobby.hooks.mcp_result import mcp_call_succeeded
from gobby.utils.session_context import (
    SessionContext,
    reset_session_context,
    set_session_context,
)

# Type alias for the call_tool function signature used by MCPClientManager
CallToolFn = Callable[[str, str, dict[str, Any]], Coroutine[Any, Any, Any]]


async def dispatch_mcp_calls(
    mcp_calls: list[dict[str, Any]],
    event: HookEvent,
    call_tool_fn: CallToolFn,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Dispatch ``mcp_call`` effects from a rule engine evaluation.

    Each call dict has the shape::

        {"server": "gobby-tasks", "tool": "create_task",
         "arguments": {...}, "background": True}

    Context injection:
    - ``session_id`` is injected from ``event.metadata["_platform_session_id"]``
      when not already present in the call's arguments.

    Args:
        mcp_calls: List of effect dicts from ``response.metadata["mcp_calls"]``.
        event: The originating HookEvent (for context injection).
        call_tool_fn: Async callable ``(server, tool, args) -> result``,
            typically ``mcp_manager.call_tool``.
        logger: Logger for diagnostics.

    Returns:
        Captured results for calls using ``inject_result``,
        ``block_on_failure``, or ``block_on_success``.
    """
    dispatch_results: list[dict[str, Any]] = []
    for call in mcp_calls:
        server = call.get("server")
        tool = call.get("tool")
        arguments = dict(call.get("arguments") or {})
        background = call.get("background", False)
        inject_result = call.get("inject_result", False)
        block_on_failure = call.get("block_on_failure", False)
        block_on_success = call.get("block_on_success", False)
        needs_capture = inject_result or block_on_failure or block_on_success

        if not server or not tool:
            logger.warning(
                "dispatch_mcp_calls: skipping call with missing server or tool: %s", call
            )
            continue

        # Inject event context into arguments.
        # Skip the call when no platform session_id could be resolved —
        # downstream lifecycle tools require a valid
        # session_id and fail with "session_id is required" when passed None.
        if "session_id" not in arguments:
            platform_sid = event.metadata.get("_platform_session_id")
            if isinstance(platform_sid, str) and platform_sid:
                arguments["session_id"] = platform_sid
            else:
                logger.warning(
                    "dispatch_mcp_calls: no platform session_id resolved for "
                    "%s/%s (event=%s, external_session_id=%s); skipping call",
                    server,
                    tool,
                    event.event_type,
                    event.session_id,
                )
                continue
        if arguments.get("prompt_text") is None:
            arguments.pop("prompt_text", None)
            event_prompt = event.data.get("prompt") if event.data else None
            if isinstance(event_prompt, str):
                arguments["prompt_text"] = event_prompt
        if "project_path" not in arguments and event.metadata.get("project_path"):
            arguments["project_path"] = event.metadata["project_path"]
        if "query" not in arguments and arguments.get("prompt_text"):
            arguments["query"] = arguments["prompt_text"]

        if needs_capture:
            try:
                result = await asyncio.wait_for(
                    _safe_call(call_tool_fn, server, tool, arguments, logger),
                    timeout=30.0,
                )
            except TimeoutError:
                logger.error("dispatch_mcp_calls: blocking call %s/%s timed out", server, tool)
                result = None
            success = mcp_call_succeeded(result)
            dispatch_results.append(
                {
                    "server": server,
                    "tool": tool,
                    "inject_result": inject_result,
                    "block_on_failure": block_on_failure,
                    "block_on_success": block_on_success,
                    "success": success,
                    "result": result,
                }
            )
            if block_on_failure and not success:
                break
            continue

        if background:
            create_background_task(_safe_call(call_tool_fn, server, tool, arguments, logger))
        else:
            try:
                await asyncio.wait_for(
                    _safe_call(call_tool_fn, server, tool, arguments, logger),
                    timeout=30.0,
                )
            except TimeoutError:
                logger.error("dispatch_mcp_calls: blocking call %s/%s timed out", server, tool)

    return dispatch_results


async def _safe_call(
    call_tool_fn: CallToolFn,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    logger: logging.Logger,
) -> Any:
    """Execute a single MCP call, logging errors without propagating."""
    session_token = _set_session_context_from_arguments(arguments)
    try:
        result = await call_tool_fn(server, tool, arguments)
        if not mcp_call_succeeded(result):
            logger.warning(
                "dispatch_mcp_calls: %s/%s returned failure: %s",
                server,
                tool,
                result.get("error", "unknown") if isinstance(result, dict) else "no result",
            )
        return result
    except Exception as exc:
        logger.exception("dispatch_mcp_calls: %s/%s failed: %s", server, tool, exc)
        return {"success": False, "error": str(exc)}
    finally:
        if session_token is not None:
            reset_session_context(session_token)


def _set_session_context_from_arguments(
    arguments: dict[str, Any],
) -> Token[SessionContext | None] | None:
    """Seed SessionContext from MCP arguments when a non-empty session_id is present."""
    session_id = arguments.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    return set_session_context(SessionContext(session_id=session_id))
