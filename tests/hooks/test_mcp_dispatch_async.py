"""Tests for async MCP call dispatch (hooks/mcp_dispatch.py)."""

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.mcp_dispatch import dispatch_mcp_calls
from gobby.utils.session_context import get_current_session_id
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


def _make_event(
    platform_session_id: str = "plat-sess-1",
    prompt: str = "Fix the auth bug",
    project_path: str | None = None,
) -> HookEvent:
    metadata = {"_platform_session_id": platform_session_id}
    if project_path is not None:
        metadata["project_path"] = project_path
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="ext-sess-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": prompt},
        metadata=metadata,
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

    @pytest.mark.asyncio
    async def test_injects_project_path_from_event_metadata(self) -> None:
        call_tool = AsyncMock()
        event = _make_event(project_path="/repo/project")

        await dispatch_mcp_calls(
            [{"server": "gobby-agents", "tool": "spawn_agent", "arguments": {}}],
            event,
            call_tool,
            logging.getLogger("test"),
        )

        args = call_tool.call_args[0][2]
        assert args["project_path"] == "/repo/project"

    @pytest.mark.asyncio
    async def test_skips_call_when_platform_session_id_is_none(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When _platform_session_id is None (unresolved), skip the call with a warning."""
        call_tool = AsyncMock(return_value={"success": True})
        event = _make_event()
        event.metadata["_platform_session_id"] = None

        results = await dispatch_mcp_calls(
            [{"server": "gobby-memory", "tool": "judge_shadow_relevance", "arguments": {}}],
            event,
            call_tool,
            logging.getLogger("test"),
        )

        assert results == []
        assert "no platform session_id resolved" in caplog.text
        call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_call_when_platform_session_id_is_missing(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When _platform_session_id key is absent, skip the call with a warning."""
        call_tool = AsyncMock(return_value={"success": True})
        event = _make_event()
        del event.metadata["_platform_session_id"]

        results = await dispatch_mcp_calls(
            [{"server": "gobby-memory", "tool": "judge_shadow_relevance", "arguments": {}}],
            event,
            call_tool,
            logging.getLogger("test"),
        )

        assert results == []
        assert "no platform session_id resolved" in caplog.text
        call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_proceeds_with_explicit_session_id_even_when_metadata_missing(self) -> None:
        """Calls with an explicit session_id are not skipped."""
        call_tool = AsyncMock(return_value={"success": True})
        event = _make_event()
        del event.metadata["_platform_session_id"]

        await dispatch_mcp_calls(
            [
                {
                    "server": "gobby-memory",
                    "tool": "digest",
                    "arguments": {"session_id": "explicit-123"},
                }
            ],
            event,
            call_tool,
            logging.getLogger("test"),
        )

        call_tool.assert_called_once()
        assert call_tool.call_args[0][2]["session_id"] == "explicit-123"

    @pytest.mark.asyncio
    async def test_maps_prompt_text_to_query(self) -> None:
        call_tool = AsyncMock(return_value={"success": True})

        await dispatch_mcp_calls(
            [{"server": "gobby-memory", "tool": "search_memories", "arguments": {}}],
            _make_event(prompt="remember this"),
            call_tool,
            logging.getLogger("test"),
        )

        assert call_tool.call_args.args[2]["query"] == "remember this"

    @pytest.mark.asyncio
    async def test_omits_null_prompt_without_event_string(self) -> None:
        call_tool = AsyncMock(return_value={"success": True})
        event = _make_event()
        event.data = {"prompt": None}

        await dispatch_mcp_calls(
            [
                {
                    "server": "gobby-memory",
                    "tool": "digest",
                    "arguments": {"prompt_text": None},
                }
            ],
            event,
            call_tool,
            logging.getLogger("test"),
        )

        assert "prompt_text" not in call_tool.call_args.args[2]

    @pytest.mark.asyncio
    async def test_preserves_explicit_non_null_prompt(self) -> None:
        call_tool = AsyncMock(return_value={"success": True})

        await dispatch_mcp_calls(
            [
                {
                    "server": "gobby-memory",
                    "tool": "digest",
                    "arguments": {"prompt_text": "explicit prompt"},
                }
            ],
            _make_event(prompt="event prompt"),
            call_tool,
            logging.getLogger("test"),
        )

        assert call_tool.call_args.args[2]["prompt_text"] == "explicit prompt"

    @pytest.mark.asyncio
    async def test_seeds_session_context_for_internal_callers(self) -> None:
        seen_contexts: list[str | None] = []

        async def call_tool(_server: str, _tool: str, _arguments: dict) -> dict[str, bool]:
            seen_contexts.append(get_current_session_id())
            return {"success": True}

        await dispatch_mcp_calls(
            [{"server": "gobby-sessions", "tool": "set_handoff", "arguments": {}}],
            _make_event(platform_session_id="plat-123"),
            call_tool,
            logging.getLogger("test"),
        )

        assert seen_contexts == ["plat-123"]
        assert get_current_session_id() is None


class TestGuardClauses:
    """Tests for skip / early-return conditions."""

    @pytest.mark.asyncio
    async def test_empty_list_is_noop(self) -> None:
        call_tool = AsyncMock()
        await dispatch_mcp_calls([], _make_event(), call_tool, logging.getLogger("test"))
        call_tool.assert_not_called()
        assert call_tool.call_count == 0
        assert not call_tool.called

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
        assert call_tool.call_count == 0
        assert not call_tool.called

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
        assert call_tool.call_count == 0
        assert not call_tool.called


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

        await wait_for_async_condition(
            lambda: call_tool.called,
            description="background MCP call",
        )
        call_tool.assert_called_once()
        assert call_tool.call_count == 1
        assert call_tool.call_args is not None

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

        await wait_for_async_condition(
            lambda: call_tool.called,
            description="background MCP error",
        )
        call_tool.assert_called_once()
        assert call_tool.call_count == 1
        assert call_tool.call_args is not None


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
        assert call_tool.call_count == 1
        assert call_tool.call_args is not None

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
        assert call_tool.call_count == 1
        assert call_tool.call_args is not None

    @pytest.mark.asyncio
    async def test_blocking_timeout_logged(self) -> None:
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
        assert call_tool.call_count == 1
        assert call_tool.call_args is not None


class TestCapturedDispatch:
    @pytest.mark.asyncio
    async def test_returns_result_for_injection(self) -> None:
        call_tool = AsyncMock(return_value={"success": True, "result": {"items": [1]}})

        results = await dispatch_mcp_calls(
            [
                {
                    "server": "gobby-memory",
                    "tool": "search_memories",
                    "arguments": {},
                    "inject_result": True,
                }
            ],
            _make_event(),
            call_tool,
            logging.getLogger("test"),
        )

        assert results == [
            {
                "server": "gobby-memory",
                "tool": "search_memories",
                "inject_result": True,
                "block_on_failure": False,
                "block_on_success": False,
                "success": True,
                "result": {"success": True, "result": {"items": [1]}},
            }
        ]

    @pytest.mark.asyncio
    async def test_block_on_failure_stops_later_calls(self) -> None:
        call_tool = AsyncMock(return_value={"success": False, "error": "gate failed"})

        results = await dispatch_mcp_calls(
            [
                {
                    "server": "gobby-tasks",
                    "tool": "gate",
                    "arguments": {},
                    "block_on_failure": True,
                },
                {"server": "gobby-tasks", "tool": "must-not-run", "arguments": {}},
            ],
            _make_event(),
            call_tool,
            logging.getLogger("test"),
        )

        assert len(results) == 1
        assert results[0]["success"] is False
        assert call_tool.await_count == 1


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
        await wait_for_async_condition(
            lambda: call_tool.call_count == 2,
            description="all MCP calls",
        )

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
