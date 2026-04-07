"""Tests for CodexCLIChatSession -- Codex CLI-backed web chat sessions.

Covers:
- Provider attribute
- Construction and field defaults
- Start (client creation, thread start, resume)
- send_message event translation (TextChunk, ToolCallEvent, ToolResultEvent, DoneEvent)
- Stop
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.codex_impl.types import CodexThread, CodexTurn
from gobby.llm.claude_models import DoneEvent, TextChunk, ToolCallEvent, ToolResultEvent
from gobby.servers.codex_cli_chat_session import CodexCLIChatSession

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_client(thread_id: str = "thread-abc") -> AsyncMock:
    """Create a mock CodexAppServerClient."""
    client = AsyncMock()
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.register_approval_handler = MagicMock()
    client.add_notification_handler = MagicMock()
    client.remove_notification_handler = MagicMock()

    thread = CodexThread(id=thread_id, preview="test")
    client.start_thread = AsyncMock(return_value=thread)
    client.resume_thread = AsyncMock(return_value=thread)

    turn = CodexTurn(id="turn-1", thread_id=thread_id)
    client.start_turn = AsyncMock(return_value=turn)
    client.interrupt_turn = AsyncMock()

    return client


def _make_session(**overrides: Any) -> CodexCLIChatSession:
    """Create a CodexCLIChatSession with test defaults."""
    defaults: dict[str, Any] = {
        "conversation_id": "test-conv-456",
    }
    defaults.update(overrides)
    return CodexCLIChatSession(**defaults)


def _wire_turn_events(
    client: AsyncMock,
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    """Set up notification handlers to fire events when start_turn is called."""
    handlers: dict[str, Any] = {}

    def _capture(method: str, handler: Any) -> None:
        handlers[method] = handler

    client.add_notification_handler.side_effect = _capture

    original = client.start_turn

    async def _start_turn_fire(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        for method, params in events:
            if method in handlers:
                handlers[method](method, params)
        return result

    client.start_turn = AsyncMock(side_effect=_start_turn_fire)


# ---------------------------------------------------------------------------
# Provider attribute
# ---------------------------------------------------------------------------


class TestProvider:
    def test_provider_is_codex(self) -> None:
        session = _make_session()
        assert session.provider == "codex"

    def test_provider_not_settable_via_init(self) -> None:
        """provider is a non-init field, always 'codex'."""
        session = _make_session()
        assert session.provider == "codex"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_fields(self) -> None:
        session = _make_session()
        assert session.conversation_id == "test-conv-456"
        assert session.db_session_id is None
        assert session.chat_mode == "plan"
        assert session.is_connected is False
        assert session.model is None
        assert session.has_pending_question is False
        assert session.has_pending_approval is False
        assert session.has_pending_plan is False

    def test_custom_fields(self) -> None:
        session = _make_session(
            db_session_id="db-456",
            seq_num=3,
            project_id="proj-2",
            chat_mode="accept_edits",
        )
        assert session.db_session_id == "db-456"
        assert session.seq_num == 3
        assert session.project_id == "proj-2"
        assert session.chat_mode == "accept_edits"


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


class TestStart:
    @pytest.mark.asyncio
    async def test_start_creates_client_and_thread(self) -> None:
        client = _mock_client(thread_id="thread-xyz")
        session = _make_session(project_path="/tmp/project")

        with (
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAdapter.is_codex_available",
                return_value=True,
            ),
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAppServerClient",
                return_value=client,
            ),
        ):
            await session.start(model="gpt-4.1")

        assert session.is_connected
        assert session.sdk_session_id == "thread-xyz"
        assert session.model == "gpt-4.1"
        client.start.assert_awaited_once()
        client.start_thread.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_with_resume(self) -> None:
        client = _mock_client(thread_id="resumed-thread")
        session = _make_session(resume_session_id="old-thread-id")

        with (
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAdapter.is_codex_available",
                return_value=True,
            ),
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAppServerClient",
                return_value=client,
            ),
        ):
            await session.start()

        client.resume_thread.assert_awaited_once_with("old-thread-id")
        client.start_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_with_thread_id(self) -> None:
        """thread_id init parameter triggers resume."""
        client = _mock_client(thread_id="existing-thread")
        session = CodexCLIChatSession(
            conversation_id="conv-tid",
            _thread_id="existing-thread",
        )

        with (
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAdapter.is_codex_available",
                return_value=True,
            ),
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAppServerClient",
                return_value=client,
            ),
        ):
            await session.start()

        client.resume_thread.assert_awaited_once_with("existing-thread")

    @pytest.mark.asyncio
    async def test_start_raises_when_codex_not_available(self) -> None:
        session = _make_session()

        with patch(
            "gobby.servers.codex_cli_chat_session.CodexAdapter.is_codex_available",
            return_value=False,
        ):
            with pytest.raises(RuntimeError, match="Codex CLI not found"):
                await session.start()


# ---------------------------------------------------------------------------
# send_message event translation
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_text_chunks_from_deltas(self) -> None:
        """Delta notifications become TextChunk events."""
        client = _mock_client()
        _wire_turn_events(
            client,
            [
                ("item/agentMessage/delta", {"delta": "Hello "}),
                ("item/agentMessage/delta", {"delta": "world!"}),
                ("turn/completed", {}),
            ],
        )

        session = _make_session(project_path="/tmp/test")
        with (
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAdapter.is_codex_available",
                return_value=True,
            ),
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAppServerClient",
                return_value=client,
            ),
        ):
            await session.start()
            events = [e async for e in session.send_message("hi")]

        text_events = [e for e in events if isinstance(e, TextChunk)]
        assert len(text_events) == 2
        assert text_events[0].content == "Hello "
        assert text_events[1].content == "world!"
        assert isinstance(events[-1], DoneEvent)

    @pytest.mark.asyncio
    async def test_tool_call_and_result_events(self) -> None:
        """Tool item events become ToolCallEvent and ToolResultEvent."""
        client = _mock_client()
        _wire_turn_events(
            client,
            [
                (
                    "item/started",
                    {
                        "item": {
                            "id": "item-1",
                            "type": "commandExecution",
                            "metadata": {"command": "ls"},
                        }
                    },
                ),
                (
                    "item/completed",
                    {
                        "item": {
                            "id": "item-1",
                            "type": "commandExecution",
                            "content": "file.txt",
                            "status": "completed",
                            "metadata": {},
                        }
                    },
                ),
                ("turn/completed", {}),
            ],
        )

        session = _make_session(project_path="/tmp/test")
        with (
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAdapter.is_codex_available",
                return_value=True,
            ),
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAppServerClient",
                return_value=client,
            ),
        ):
            await session.start()
            events = [e async for e in session.send_message("list files")]

        tool_calls = [e for e in events if isinstance(e, ToolCallEvent)]
        tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tool_calls) == 1
        assert tool_calls[0].tool_name == "Bash"  # commandExecution -> Bash
        assert len(tool_results) == 1
        assert tool_results[0].success is True
        assert tool_results[0].result == "file.txt"

        done = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done) == 1
        assert done[0].tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_not_connected_raises(self) -> None:
        """send_message raises when session is not started."""
        session = _make_session()
        with pytest.raises(RuntimeError, match="not connected"):
            async for _ in session.send_message("hi"):
                pass


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_disconnects(self) -> None:
        client = _mock_client()
        session = _make_session(project_path="/tmp/test")

        with (
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAdapter.is_codex_available",
                return_value=True,
            ),
            patch(
                "gobby.servers.codex_cli_chat_session.CodexAppServerClient",
                return_value=client,
            ),
        ):
            await session.start()
            assert session.is_connected
            await session.stop()

        assert not session.is_connected
        client.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_drain_is_noop(self) -> None:
        """drain_pending_response is a no-op for Codex CLI."""
        session = _make_session()
        await session.drain_pending_response()  # Should not raise
