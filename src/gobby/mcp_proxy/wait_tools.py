"""Helpers for MCP wrapper tools that intentionally wait."""

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

from mcp.server.fastmcp import Context

WAIT_TOOL_NAMES = (
    "wait_for_task",
    "wait_for_any_task",
    "wait_for_all_tasks",
    "wait_for_agent",
)
WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS = 15.0
WAIT_TOOL_HTTP_TIMEOUT_BUFFER_SECONDS = 30.0
MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS = 60.0
MCP_WRAPPER_EXTENDED_TOOL_TIMEOUT_SECONDS = 90.0
WAIT_TOOL_WRAPPER_GRACE_SECONDS = 5.0
EXTENDED_TIMEOUT_TOOL_NAMES = (
    "close_task",
    "expand_task",
    "apply_tdd",
    "merge_resolve",
    "suggest_next_task",
    "compact_self",
)
CLIENT_GUARDED_TOOL_NAMES = (*WAIT_TOOL_NAMES, *EXTENDED_TIMEOUT_TOOL_NAMES)
HEARTBEAT_TOOL_NAMES = (*WAIT_TOOL_NAMES, *EXTENDED_TIMEOUT_TOOL_NAMES)

logger = logging.getLogger("gobby.mcp.wait_tools")


def _wrapper_timeout_result(tool_name: str, timeout: float) -> dict[str, Any]:
    return {
        "success": True,
        "completed": False,
        "timeout_seconds": timeout,
        "effective_timeout_seconds": timeout,
        "mcp_wrapper_timeout": True,
        "background_call_continues": True,
        "tool_name": tool_name,
    }


def _consume_background_result(task: asyncio.Task[dict[str, Any]]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("Background MCP wrapper call failed after timeout: %s", exc)


async def _await_with_guard(
    tool_call: Awaitable[dict[str, Any]],
    *,
    tool_name: str,
    timeout: float | None,
) -> dict[str, Any]:
    if tool_name not in CLIENT_GUARDED_TOOL_NAMES or timeout is None:
        return await tool_call
    task = asyncio.ensure_future(tool_call)
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout + WAIT_TOOL_WRAPPER_GRACE_SECONDS,
        )
    except TimeoutError:
        task.add_done_callback(_consume_background_result)
        return _wrapper_timeout_result(tool_name, timeout)


async def call_with_wait_heartbeat(
    tool_call: Awaitable[dict[str, Any]],
    *,
    ctx: Context[Any, Any, Any] | None,
    tool_name: str,
    timeout: float | None,
) -> dict[str, Any]:
    """Keep stdio transport active while a wait-capable proxied tool blocks."""
    if ctx is None or tool_name not in HEARTBEAT_TOOL_NAMES:
        return await _await_with_guard(tool_call, tool_name=tool_name, timeout=timeout)

    stop_event = asyncio.Event()

    async def _heartbeat() -> None:
        elapsed = 0.0
        while True:
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS,
                )
                return
            except TimeoutError:
                elapsed += WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS
                progress = min(elapsed, timeout) if timeout is not None else elapsed
                await ctx.report_progress(
                    progress=progress,
                    total=timeout,
                    message=f"{tool_name} still waiting for daemon result",
                )

    heartbeat_task = asyncio.create_task(_heartbeat(), name=f"{tool_name}-heartbeat")
    try:
        return await _await_with_guard(tool_call, tool_name=tool_name, timeout=timeout)
    finally:
        stop_event.set()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
