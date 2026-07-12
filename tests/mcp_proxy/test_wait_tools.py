"""Tests for MCP wait-tool heartbeat handling."""

import asyncio
from collections.abc import Awaitable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.wait_tools import call_with_wait_heartbeat

pytestmark = pytest.mark.unit


def _failing_progress_context(attempted: asyncio.Event) -> MagicMock:
    async def fail_progress(**_kwargs: Any) -> None:
        attempted.set()
        raise BrokenPipeError("client disconnected")

    ctx = MagicMock()
    ctx.report_progress = AsyncMock(side_effect=fail_progress)
    return ctx


@pytest.mark.asyncio
async def test_heartbeat_failure_does_not_replace_tool_result() -> None:
    """A failed progress report cannot mask a successful wait-tool result."""
    heartbeat_attempted = asyncio.Event()
    ctx = _failing_progress_context(heartbeat_attempted)

    async def tool_call() -> dict[str, bool]:
        await heartbeat_attempted.wait()
        return {"success": True}

    with patch("gobby.mcp_proxy.wait_tools.WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS", 0.01):
        result = await call_with_wait_heartbeat(
            tool_call(),
            ctx=ctx,
            tool_name="wait_for_agent",
            timeout=1.0,
        )

    assert result == {"success": True}
    ctx.report_progress.assert_awaited()


@pytest.mark.asyncio
async def test_heartbeat_failure_does_not_replace_tool_exception() -> None:
    """A failed progress report cannot mask the wait tool's own exception."""
    heartbeat_attempted = asyncio.Event()
    ctx = _failing_progress_context(heartbeat_attempted)

    async def tool_call() -> dict[str, Any]:
        await heartbeat_attempted.wait()
        raise ValueError("tool failed")

    call: Awaitable[dict[str, Any]] = tool_call()
    with (
        patch("gobby.mcp_proxy.wait_tools.WAIT_TOOL_HEARTBEAT_INTERVAL_SECONDS", 0.01),
        pytest.raises(ValueError, match="tool failed"),
    ):
        await call_with_wait_heartbeat(
            call,
            ctx=ctx,
            tool_name="wait_for_agent",
            timeout=1.0,
        )

    ctx.report_progress.assert_awaited()
