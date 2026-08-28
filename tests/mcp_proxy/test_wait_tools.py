"""Tests for MCP wait-tool heartbeat handling."""

import asyncio
from collections.abc import Awaitable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS,
    WAIT_TOOL_NAMES,
    call_with_wait_heartbeat,
    clamp_wait_tool_timeout,
    prepare_client_guard,
    wait_tool_timeout_limit,
)

pytestmark = pytest.mark.unit


def test_wait_tool_names_only_include_implemented_tools() -> None:
    assert WAIT_TOOL_NAMES == ("wait_for_output",)


def test_wait_for_agent_uses_ordinary_client_guard() -> None:
    arguments = {"run_id": "run-123", "timeout_seconds": 600}

    guard = prepare_client_guard(tool_name="wait_for_agent", arguments=arguments)

    assert guard.arguments is arguments
    assert guard.timeout is None
    assert guard.requested_timeout_seconds is None
    assert guard.effective_timeout_seconds is None
    assert guard.wait_timeout_capped is False


@pytest.mark.parametrize("tool_name", WAIT_TOOL_NAMES)
def test_wait_tool_timeout_caps_match_wrapper(tool_name: str) -> None:
    assert wait_tool_timeout_limit(tool_name) == MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS
    assert (
        clamp_wait_tool_timeout(
            tool_name,
            MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS + 1,
            default=0.0,
        )
        == MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS
    )


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
            tool_name="wait_for_output",
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
            tool_name="wait_for_output",
            timeout=1.0,
        )

    ctx.report_progress.assert_awaited()
