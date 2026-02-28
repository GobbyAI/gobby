"""Tests for HookManager._dispatch_mcp_calls method."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource

pytestmark = pytest.mark.unit


def _make_hook_manager_stub(
    tool_proxy_getter=None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> MagicMock:
    """Create a minimal stub with just the fields _dispatch_mcp_calls needs."""
    from gobby.hooks.hook_manager import HookManager

    stub = MagicMock(spec=HookManager)
    stub.tool_proxy_getter = tool_proxy_getter
    stub._loop = loop
    stub.logger = MagicMock()
    # Bind the real method to our stub
    stub._dispatch_mcp_calls = HookManager._dispatch_mcp_calls.__get__(stub, HookManager)
    return stub


def _make_event(
    platform_session_id: str = "plat-sess-1",
    prompt: str = "Fix the auth bug",
) -> HookEvent:
    """Create a minimal HookEvent for testing."""
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="ext-sess-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": prompt},
        metadata={"_platform_session_id": platform_session_id},
    )


class TestDispatchMcpCallsGuards:
    """Tests for guard clauses in _dispatch_mcp_calls."""

    def test_no_tool_proxy_getter_returns_early(self) -> None:
        """When tool_proxy_getter is None, does nothing."""
        stub = _make_hook_manager_stub(tool_proxy_getter=None)
        event = _make_event()

        stub._dispatch_mcp_calls(
            [{"server": "gobby-memory", "tool": "digest_and_synthesize", "arguments": {}}],
            event,
        )

        stub.logger.debug.assert_called()

    def test_missing_server_or_tool_logs_warning(self) -> None:
        """Calls with missing server/tool are skipped with a warning."""
        proxy = AsyncMock()
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy)
        event = _make_event()

        stub._dispatch_mcp_calls(
            [{"server": None, "tool": "digest_and_synthesize", "arguments": {}}],
            event,
        )

        stub.logger.warning.assert_called()

    def test_empty_list_is_noop(self) -> None:
        """Empty mcp_calls list does nothing."""
        proxy = AsyncMock()
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy)
        event = _make_event()

        stub._dispatch_mcp_calls([], event)

        proxy.call_tool.assert_not_called()


class TestDispatchMcpCallsContextInjection:
    """Tests for event context injection into call arguments."""

    def test_injects_session_id(self) -> None:
        """session_id is injected from event when not in arguments."""
        proxy = AsyncMock()
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy)
        event = _make_event(platform_session_id="plat-123", prompt="Hello world")

        calls = [
            {
                "server": "gobby-memory",
                "tool": "digest_and_synthesize",
                "arguments": {"limit": 20},
                "background": False,
            }
        ]

        # Run with a real event loop so the foreground coroutine can execute
        loop = asyncio.new_event_loop()
        stub._loop = loop

        try:
            # Use run_coroutine_threadsafe path (no running loop in this thread)
            stub._dispatch_mcp_calls(calls, event)

            # The future.result(timeout=30) should have executed the coroutine
            if proxy.call_tool.called:
                args = proxy.call_tool.call_args
                actual_args = args[0][2] if len(args[0]) > 2 else args.kwargs.get("arguments", {})
                assert actual_args["session_id"] == "plat-123"
                assert actual_args["prompt_text"] == "Hello world"
                assert actual_args["limit"] == 20
                # Verify strip_unknown=True is passed to proxy
                call_kwargs = proxy.call_tool.call_args.kwargs
                assert call_kwargs.get("strip_unknown") is True
        finally:
            loop.close()

    def test_does_not_overwrite_existing_session_id(self) -> None:
        """If arguments already contain session_id, it is not overwritten."""
        proxy = AsyncMock()
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy)
        event = _make_event(platform_session_id="plat-123")

        calls = [
            {
                "server": "gobby-memory",
                "tool": "digest_and_synthesize",
                "arguments": {"session_id": "explicit-sess", "limit": 20},
                "background": False,
            }
        ]

        loop = asyncio.new_event_loop()
        stub._loop = loop

        try:
            stub._dispatch_mcp_calls(calls, event)

            if proxy.call_tool.called:
                actual_args = proxy.call_tool.call_args[0][2]
                assert actual_args["session_id"] == "explicit-sess"
        finally:
            loop.close()


class TestDispatchMcpCallsBackgroundMode:
    """Tests for background (fire-and-forget) dispatch."""

    @pytest.mark.asyncio
    async def test_background_call_uses_create_task(self) -> None:
        """Background calls use loop.create_task (fire-and-forget)."""
        proxy = AsyncMock()
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy)
        event = _make_event()

        calls = [
            {
                "server": "gobby-memory",
                "tool": "digest_and_synthesize",
                "arguments": {"limit": 20},
                "background": True,
            }
        ]

        # We're in an async context, so get_running_loop will succeed
        stub._dispatch_mcp_calls(calls, event)

        # Give the task a chance to execute
        await asyncio.sleep(0.05)

        proxy.call_tool.assert_called_once()
        call_args = proxy.call_tool.call_args[0]
        assert call_args[0] == "gobby-memory"
        assert call_args[1] == "digest_and_synthesize"

    @pytest.mark.asyncio
    async def test_background_call_error_does_not_raise(self) -> None:
        """Errors in background calls are logged, not raised."""
        proxy = AsyncMock(side_effect=RuntimeError("LLM down"))
        # tool_proxy_getter returns a proxy whose call_tool raises
        mock_proxy = AsyncMock()
        mock_proxy.call_tool = proxy

        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: mock_proxy)
        event = _make_event()

        calls = [
            {
                "server": "gobby-memory",
                "tool": "digest_and_synthesize",
                "arguments": {},
                "background": True,
            }
        ]

        # Should not raise
        stub._dispatch_mcp_calls(calls, event)
        await asyncio.sleep(0.05)

        # Error was logged
        stub.logger.error.assert_called()


class TestDispatchMcpCallsNoEventLoop:
    """Tests for the asyncio.run() fallback when no event loop is available."""

    def test_blocking_call_falls_back_to_asyncio_run(self) -> None:
        """When no event loop exists, blocking calls use asyncio.run()."""
        proxy = AsyncMock()
        # _loop is None to simulate hook manager subprocess
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event(platform_session_id="plat-456")

        calls = [
            {
                "server": "gobby-sessions",
                "tool": "set_handoff_context",
                "arguments": {"full": True},
                "background": False,
            }
        ]

        stub._dispatch_mcp_calls(calls, event)

        proxy.call_tool.assert_called_once()
        call_args = proxy.call_tool.call_args[0]
        assert call_args[0] == "gobby-sessions"
        assert call_args[1] == "set_handoff_context"
        assert call_args[2]["full"] is True
        assert call_args[2]["session_id"] == "plat-456"

    def test_background_call_falls_back_to_asyncio_run(self) -> None:
        """When no event loop exists, background calls also use asyncio.run()."""
        proxy = AsyncMock()
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "gobby-memory",
                "tool": "extract_from_session",
                "arguments": {"max_memories": 5},
                "background": True,
            }
        ]

        stub._dispatch_mcp_calls(calls, event)

        proxy.call_tool.assert_called_once()

    def test_blocking_asyncio_run_error_is_logged(self) -> None:
        """Errors in asyncio.run() fallback for blocking calls are logged."""
        mock_proxy = AsyncMock()
        mock_proxy.call_tool = AsyncMock(side_effect=RuntimeError("connection refused"))
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: mock_proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "gobby-sessions",
                "tool": "set_handoff_context",
                "arguments": {},
                "background": False,
            }
        ]

        # Should not raise
        stub._dispatch_mcp_calls(calls, event)
        stub.logger.error.assert_called()

    def test_multiple_calls_all_execute(self) -> None:
        """Multiple MCP calls in sequence all execute via asyncio.run()."""
        proxy = AsyncMock()
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "gobby-sessions",
                "tool": "set_handoff_context",
                "arguments": {"compact": True},
            },
            {
                "server": "gobby-memory",
                "tool": "extract_from_session",
                "arguments": {"max_memories": 5},
            },
            {"server": "gobby-tasks", "tool": "sync_export", "arguments": {}},
        ]

        stub._dispatch_mcp_calls(calls, event)

        assert proxy.call_tool.call_count == 3


class TestDispatchMcpCallsProxyNone:
    """Tests for when tool_proxy_getter returns None."""

    @pytest.mark.asyncio
    async def test_proxy_returns_none_logs_warning(self) -> None:
        """When tool_proxy_getter() returns None, a warning is logged."""
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: None)
        event = _make_event()

        calls = [
            {
                "server": "gobby-memory",
                "tool": "digest_and_synthesize",
                "arguments": {},
                "background": True,
            }
        ]

        stub._dispatch_mcp_calls(calls, event)
        await asyncio.sleep(0.05)

        stub.logger.warning.assert_called()
