"""Tests for HookManager._dispatch_mcp_calls method."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from tests._timing import wait_for_async_condition

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
    # Bind the real methods to our stub
    stub._dispatch_mcp_calls = HookManager._dispatch_mcp_calls.__get__(stub, HookManager)
    stub._run_coro_blocking = HookManager._run_coro_blocking.__get__(stub, HookManager)
    stub._proxy_self_call = HookManager._proxy_self_call.__get__(stub, HookManager)
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
            [{"server": "gobby-memory", "tool": "search_memories", "arguments": {}}],
            event,
        )

        stub.logger.debug.assert_called()
        assert stub.logger.debug.call_count >= 1
        assert stub.logger.debug.call_args is not None

    def test_missing_server_or_tool_logs_warning(self) -> None:
        """Calls with missing server/tool are skipped with a warning."""
        proxy = AsyncMock()
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy)
        event = _make_event()

        stub._dispatch_mcp_calls(
            [{"server": None, "tool": "search_memories", "arguments": {}}],
            event,
        )

        stub.logger.warning.assert_called()
        assert stub.logger.warning.call_count >= 1
        assert stub.logger.warning.call_args is not None

    def test_empty_list_is_noop(self) -> None:
        """Empty mcp_calls list does nothing."""
        proxy = AsyncMock()
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy)
        event = _make_event()

        stub._dispatch_mcp_calls([], event)

        proxy.call_tool.assert_not_called()
        assert proxy.call_tool.call_count == 0
        assert not proxy.call_tool.called


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
                "tool": "search_memories",
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
                "tool": "search_memories",
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
                "tool": "search_memories",
                "arguments": {"limit": 20},
                "background": True,
            }
        ]

        # We're in an async context, so get_running_loop will succeed
        stub._dispatch_mcp_calls(calls, event)

        await wait_for_async_condition(
            lambda: proxy.call_tool.called,
            description="background MCP call",
        )

        proxy.call_tool.assert_called_once()
        call_args = proxy.call_tool.call_args[0]
        assert call_args[0] == "gobby-memory"
        assert call_args[1] == "search_memories"

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
                "tool": "search_memories",
                "arguments": {},
                "background": True,
            }
        ]

        # Should not raise
        stub._dispatch_mcp_calls(calls, event)
        await wait_for_async_condition(
            lambda: stub.logger.error.called,
            description="background MCP error log",
        )

        # Error was logged
        stub.logger.error.assert_called()
        assert stub.logger.error.call_count >= 1
        assert stub.logger.error.call_args is not None


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
                "tool": "sync_export",
                "arguments": {},
                "background": True,
            }
        ]

        stub._dispatch_mcp_calls(calls, event)

        proxy.call_tool.assert_called_once()
        assert proxy.call_tool.call_count == 1
        assert proxy.call_tool.call_args is not None

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
        assert stub.logger.error.call_count >= 1
        assert stub.logger.error.call_args is not None

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
                "tool": "sync_export",
                "arguments": {},
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
                "tool": "search_memories",
                "arguments": {},
                "background": True,
            }
        ]

        stub._dispatch_mcp_calls(calls, event)
        await wait_for_async_condition(
            lambda: stub.logger.warning.called,
            description="missing proxy warning",
        )

        stub.logger.warning.assert_called()
        assert stub.logger.warning.call_count >= 1
        assert stub.logger.warning.call_args is not None


class TestDispatchMcpCallsSessionResolution:
    """Tests for external_id → platform UUID resolution in the dispatcher."""

    def _proxy_with_session_manager(
        self,
        *,
        resolve_to: str | None,
        resolve_exc=None,
    ) -> tuple[AsyncMock, MagicMock]:
        """Build an AsyncMock proxy whose session_manager resolves refs.

        The dispatcher reads ``proxy.session_manager`` directly (see
        hooks/dispatchers/mcp.py). AsyncMock auto-generates child mocks
        for any unset attribute, so the real session_manager must be
        bound to the proxy at that exact attribute path — otherwise the
        auto-generated stand-in silently absorbs the calls the test
        means to assert against.
        """
        session_manager = MagicMock()
        session_manager.db = MagicMock()
        if resolve_exc is not None:
            session_manager.resolve_session_reference.side_effect = resolve_exc
        else:
            session_manager.resolve_session_reference.return_value = resolve_to
        session = MagicMock()
        session.external_id = "ext-xyz"
        session.project_id = "proj-abc"
        session_manager.get.return_value = session

        proxy = AsyncMock()
        proxy.session_manager = session_manager
        return proxy, session_manager

    def test_dispatch_resolves_external_id_before_setting_session_context(self) -> None:
        """Caller-supplied external_id is resolved to the platform UUID before dispatch."""
        external_uuid = "11111111-1111-1111-1111-111111111111"
        platform_uuid = "22222222-2222-2222-2222-222222222222"
        proxy, session_manager = self._proxy_with_session_manager(resolve_to=platform_uuid)
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy)
        event = _make_event(platform_session_id="ignored-default")

        calls = [
            {
                "server": "gobby-memory",
                "tool": "search_memories",
                "arguments": {"session_id": external_uuid, "limit": 5},
                "background": False,
            }
        ]

        loop = asyncio.new_event_loop()
        stub._loop = loop
        try:
            stub._dispatch_mcp_calls(calls, event)
        finally:
            loop.close()

        session_manager.resolve_session_reference.assert_called()
        called_kwargs = proxy.call_tool.call_args.kwargs
        assert called_kwargs.get("session_id") == platform_uuid

    def test_dispatch_unresolvable_session_id_skips_set_session_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unresolvable external_id → warning, no SessionContext planted."""
        import logging as _logging

        external_uuid = "33333333-3333-3333-3333-333333333333"
        proxy, _ = self._proxy_with_session_manager(
            resolve_to=None, resolve_exc=ValueError("Session not found")
        )
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy)
        event = _make_event(platform_session_id="ignored-default")

        calls = [
            {
                "server": "gobby-memory",
                "tool": "search_memories",
                "arguments": {"session_id": external_uuid},
                "background": False,
            }
        ]

        caplog.set_level(_logging.WARNING, logger="gobby.utils.session_context")
        loop = asyncio.new_event_loop()
        stub._loop = loop
        try:
            stub._dispatch_mcp_calls(calls, event)
        finally:
            loop.close()

        assert any("could not resolve session ref" in rec.message for rec in caplog.records)
        called_kwargs = proxy.call_tool.call_args.kwargs
        assert called_kwargs.get("session_id") is None
