"""Helpers for MCP wrapper tools that intentionally wait."""

import asyncio
from collections.abc import Awaitable
from typing import Any

from mcp.server.fastmcp import Context

WAIT_TOOL_NAMES = (
    "wait_for_task",
    "wait_for_any_task",
    "wait_for_all_tasks",
    "wait_for_agent",
)
HEARTBEAT_TOOL_NAMES = (*WAIT_TOOL_NAMES, "compact_self")
WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS = 15.0
WAIT_TOOL_HTTP_TIMEOUT_BUFFER_SECONDS = 30.0
MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS = 60.0
WAIT_TOOL_WRAPPER_GRACE_SECONDS = 5.0


def _wrapper_timeout_result(tool_name: str, timeout: float) -> dict[str, Any]:
    return {
        "success": True,
        "completed": False,
        "timeout_seconds": timeout,
        "effective_timeout_seconds": timeout,
        "mcp_wrapper_timeout": True,
        "tool_name": tool_name,
    }


async def _await_with_guard(
    tool_call: Awaitable[dict[str, Any]],
    *,
    tool_name: str,
    timeout: float | None,
) -> dict[str, Any]:
    if tool_name not in WAIT_TOOL_NAMES or timeout is None:
        return await tool_call
    try:
        return await asyncio.wait_for(tool_call, timeout + WAIT_TOOL_WRAPPER_GRACE_SECONDS)
    except TimeoutError:
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
