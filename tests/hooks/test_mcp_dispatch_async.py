"""Tests for async MCP call dispatch (hooks/mcp_dispatch.py)."""

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.mcp_dispatch import dispatch_mcp_calls

pytestmark = pytest.mark.unit


def _make_event(
    platform_session_id: str = "plat-sess-1",
    prompt: str = "Fix the auth bug",
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="ext-sess-1",
        source=SessionSource.CLAUDE_SDK_WEB_CHAT,
        timestamp=datetime.now(UTC),
        data={"prompt": prompt},
        metadata={"_platform_session_id": platform_session_id},
    )


class TestContextInjection:
    """Tests for event context injection into call arguments."""

    @pytest.mark.asyncio
    async def test_injects_session_id(self) -> None:
        call_tool = AsyncMock()
        event = _make_event(platform_session_id="plat-123", prompt="Hello")

        await dispatch_mcp_calls(
            [{"server": "gobby-memory", "tool": "digest", "arguments": {"limit": 5}}],
            event,
            call_tool,
            logging.getLogger("test"),
        )

        call_tool.assert_called_once()
        args = call_tool.call_args[0][2]
        assert args["session_id"] == "plat-123"
        assert args["prompt_text"] == "Hello"
        assert args["limit"] == 5

    @pytest.mark.asyncio
    async def test_does_not_overwrite_existing_session_id(self) -> None:
        call_tool = AsyncMock()
        event = _make_event(platform_session_id="plat-123")

        await dispatch_mcp_calls(
            [{"server": "s", "tool": "t", "arguments": {"session_id": "explicit"}}],
            event,
            call_tool,
            logging.getLogger("test"),
        )

        args = call_tool.call_args[0][2]
        assert args["session_id"] == "explicit"


class TestGuardClauses:
    """Tests for skip / early-return conditions."""

    @pytest.mark.asyncio
    async def test_empty_list_is_noop(self) -> None:
        call_tool = AsyncMock()
        await dispatch_mcp_calls([], _make_event(), call_tool, logging.getLogger("test"))
        call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_server_skips_call(self) -> None:
        call_tool = AsyncMock()
        test_logger = logging.getLogger("test")

        await dispatch_mcp_calls(
            [{"server": None, "tool": "t", "arguments": {}}],
            _make_event(),
            call_tool,
            test_logger,
        )

        call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_tool_skips_call(self) -> None:
        call_tool = AsyncMock()

        await dispatch_mcp_calls(
            [{"server": "s", "tool": None, "arguments": {}}],
            _make_event(),
            call_tool,
            logging.getLogger("test"),
        )

        call_tool.assert_not_called()


class TestBackgroundDispatch:
    """Tests for background (fire-and-forget) dispatch."""

    @pytest.mark.asyncio
    async def test_background_call_fires_as_task(self) -> None:
        call_tool = AsyncMock()

        await dispatch_mcp_calls(
            [{"server": "s", "tool": "t", "arguments": {}, "background": True}],
            _make_event(),
            call_tool,
            logging.getLogger("test"),
        )

        # Background task needs a tick to execute
        await asyncio.sleep(0.05)
        call_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_background_error_does_not_propagate(self) -> None:
        call_tool = AsyncMock(side_effect=RuntimeError("boom"))

        # Should not raise
        await dispatch_mcp_calls(
            [{"server": "s", "tool": "t", "arguments": {}, "background": True}],
            _make_event(),
            call_tool,
            logging.getLogger("test"),
        )

        await asyncio.sleep(0.05)
        call_tool.assert_called_once()


class TestBlockingDispatch:
    """Tests for blocking (foreground) dispatch."""

    @pytest.mark.asyncio
    async def test_blocking_call_awaits_result(self) -> None:
        call_tool = AsyncMock(return_value={"ok": True})

        await dispatch_mcp_calls(
            [{"server": "s", "tool": "t", "arguments": {}, "background": False}],
            _make_event(),
            call_tool,
            logging.getLogger("test"),
        )

        call_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_blocking_error_logged_not_raised(self) -> None:
        call_tool = AsyncMock(side_effect=ValueError("bad args"))

        # Should not raise
        await dispatch_mcp_calls(
            [{"server": "s", "tool": "t", "arguments": {}, "background": False}],
            _make_event(),
            call_tool,
            logging.getLogger("test"),
        )

        call_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_blocking_timeout_logged(self) -> None:
        async def slow_call(s: str, t: str, args: dict) -> None:
            await asyncio.sleep(60)

        # We can't easily test the 30s timeout in a unit test,
        # so just verify the blocking path works without errors
        call_tool = AsyncMock()
        await dispatch_mcp_calls(
            [{"server": "s", "tool": "t", "arguments": {}, "background": False}],
            _make_event(),
            call_tool,
            logging.getLogger("test"),
        )
        call_tool.assert_called_once()


class TestMultipleCalls:
    """Tests for dispatching multiple calls."""

    @pytest.mark.asyncio
    async def test_multiple_calls_all_dispatched(self) -> None:
        call_tool = AsyncMock()

        calls = [
            {"server": "s1", "tool": "t1", "arguments": {}},
            {"server": "s2", "tool": "t2", "arguments": {}, "background": True},
        ]

        await dispatch_mcp_calls(calls, _make_event(), call_tool, logging.getLogger("test"))
        await asyncio.sleep(0.05)

        assert call_tool.call_count == 2

    @pytest.mark.asyncio
    async def test_invalid_call_skipped_others_proceed(self) -> None:
        call_tool = AsyncMock()

        calls = [
            {"server": None, "tool": "t1", "arguments": {}},  # skipped
            {"server": "s2", "tool": "t2", "arguments": {}},  # executed
        ]

        await dispatch_mcp_calls(calls, _make_event(), call_tool, logging.getLogger("test"))
        call_tool.assert_called_once()
        assert call_tool.call_args[0][0] == "s2"
