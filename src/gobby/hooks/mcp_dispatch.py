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
from typing import Any

from gobby.hooks.events import HookEvent

# Type alias for the call_tool function signature used by MCPClientManager
CallToolFn = Callable[[str, str, dict[str, Any]], Coroutine[Any, Any, Any]]


async def dispatch_mcp_calls(
    mcp_calls: list[dict[str, Any]],
    event: HookEvent,
    call_tool_fn: CallToolFn,
    logger: logging.Logger,
) -> None:
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
    """
    for call in mcp_calls:
        server = call.get("server")
        tool = call.get("tool")
        arguments = dict(call.get("arguments") or {})
        background = call.get("background", False)

        if not server or not tool:
            logger.warning(
                "dispatch_mcp_calls: skipping call with missing server or tool: %s", call
            )
            continue

        # Inject event context into arguments
        if "session_id" not in arguments:
            arguments["session_id"] = event.metadata.get("_platform_session_id", "")
        if "prompt_text" not in arguments:
            arguments["prompt_text"] = event.data.get("prompt") if event.data else None

        if background:
            asyncio.create_task(_safe_call(call_tool_fn, server, tool, arguments, logger))
        else:
            try:
                await asyncio.wait_for(
                    _safe_call(call_tool_fn, server, tool, arguments, logger),
                    timeout=30.0,
                )
            except TimeoutError:
                logger.error("dispatch_mcp_calls: blocking call %s/%s timed out", server, tool)


async def _safe_call(
    call_tool_fn: CallToolFn,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Execute a single MCP call, logging errors without propagating."""
    try:
        result = await call_tool_fn(server, tool, arguments)
        if isinstance(result, dict) and result.get("success") is False:
            logger.warning(
                "dispatch_mcp_calls: %s/%s returned failure: %s",
                server,
                tool,
                result.get("error", "unknown"),
            )
    except Exception as exc:
        logger.error("dispatch_mcp_calls: %s/%s failed: %s", server, tool, exc, exc_info=True)
