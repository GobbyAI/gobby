"""Tests for GeminiCLIChatSession -- Gemini ACP-backed web chat sessions.

Covers:
- Construction and field defaults (provider="gemini")
- Start (client creation, resume)
- send_message event translation (TextChunk, DoneEvent)
- Stop
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.adapters.gemini_acp_client import StreamEvent
from gobby.llm.claude_models import DoneEvent, TextChunk
from gobby.servers.gemini_cli_chat_session import GeminiCLIChatSession

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_acp_client(
    stream_events: list[StreamEvent] | None = None,
) -> AsyncMock:
    """Create a mock GeminiACPClient."""
    client = AsyncMock()
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.is_started = True

    # send() returns an async iterator of StreamEvent
    events = list(stream_events or [])

    async def _send(message: str):  # type: ignore[no-untyped-def]
        for event in events:
            yield event

    client.send = _send

    return client


def _make_session(**overrides: Any) -> GeminiCLIChatSession:
    """Create a GeminiCLIChatSession with test defaults."""
    defaults: dict[str, Any] = {
        "conversation_id": "test-conv-gemini",
    }
    defaults.update(overrides)
    return GeminiCLIChatSession(**defaults)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_provider_is_gemini(self) -> None:
        session = _make_session()
        assert session.provider == "gemini"

    def test_default_fields(self) -> None:
        session = _make_session()
        assert session.conversation_id == "test-conv-gemini"
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
            project_id="proj-gem",
            chat_mode="accept_edits",
        )
        assert session.db_session_id == "db-456"
        assert session.seq_num == 3
        assert session.project_id == "proj-gem"
        assert session.chat_mode == "accept_edits"


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


class TestStart:
    @pytest.mark.asyncio
    async def test_start_creates_client(self) -> None:
        client = _mock_acp_client()
        session = _make_session(project_path="/tmp/project")

        with patch(
            "gobby.servers.gemini_cli_chat_session.GeminiACPClient",
            return_value=client,
        ):
            await session.start(model="gemini-2.5-pro")

        assert session.is_connected
        assert session.model == "gemini-2.5-pro"
        client.start.assert_awaited_once_with(session_id=None)

    @pytest.mark.asyncio
    async def test_start_with_resume(self) -> None:
        client = _mock_acp_client()
        session = _make_session(resume_session_id="prev-session-id")

        with patch(
            "gobby.servers.gemini_cli_chat_session.GeminiACPClient",
            return_value=client,
        ):
            await session.start()

        client.start.assert_awaited_once_with(session_id="prev-session-id")

    @pytest.mark.asyncio
    async def test_start_raises_when_cli_not_found(self) -> None:
        """FileNotFoundError from GeminiACPClient.start propagates."""
        client = AsyncMock()
        client.start = AsyncMock(side_effect=FileNotFoundError("Gemini CLI not found"))

        session = _make_session()
        with patch(
            "gobby.servers.gemini_cli_chat_session.GeminiACPClient",
            return_value=client,
        ):
            with pytest.raises(FileNotFoundError, match="Gemini CLI not found"):
                await session.start()


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_text_chunks_from_content_deltas(self) -> None:
        """content_delta StreamEvents become TextChunk ChatEvents."""
        client = _mock_acp_client(
            stream_events=[
                StreamEvent(event_type="content_delta", data={"content": "Hello "}),
                StreamEvent(event_type="content_delta", data={"content": "world!"}),
                StreamEvent(event_type="result", data={"stats": {}}),
            ]
        )

        session = _make_session(project_path="/tmp/test")
        with patch(
            "gobby.servers.gemini_cli_chat_session.GeminiACPClient",
            return_value=client,
        ):
            await session.start()
            events = [e async for e in session.send_message("hi")]

        text_events = [e for e in events if isinstance(e, TextChunk)]
        assert len(text_events) == 2
        assert text_events[0].content == "Hello "
        assert text_events[1].content == "world!"

        # Should end with DoneEvent
        assert isinstance(events[-1], DoneEvent)

    @pytest.mark.asyncio
    async def test_done_event_has_zero_tool_calls(self) -> None:
        """DoneEvent reports tool_calls_count=0 for plain text responses."""
        client = _mock_acp_client(
            stream_events=[
                StreamEvent(event_type="content_delta", data={"content": "ok"}),
                StreamEvent(event_type="result", data={}),
            ]
        )

        session = _make_session(project_path="/tmp/test")
        with patch(
            "gobby.servers.gemini_cli_chat_session.GeminiACPClient",
            return_value=client,
        ):
            await session.start()
            events = [e async for e in session.send_message("test")]

        done = [e for e in events if isinstance(e, DoneEvent)]
        assert len(done) == 1
        assert done[0].tool_calls_count == 0

    @pytest.mark.asyncio
    async def test_error_event_yields_text_chunk(self) -> None:
        """Error StreamEvents become TextChunk with error message."""
        client = _mock_acp_client(
            stream_events=[
                StreamEvent(event_type="error", data={"message": "rate limit"}),
            ]
        )

        session = _make_session(project_path="/tmp/test")
        with patch(
            "gobby.servers.gemini_cli_chat_session.GeminiACPClient",
            return_value=client,
        ):
            await session.start()
            events = [e async for e in session.send_message("boom")]

        text_events = [e for e in events if isinstance(e, TextChunk)]
        assert any("rate limit" in e.content for e in text_events)

    @pytest.mark.asyncio
    async def test_content_list_input(self) -> None:
        """Content can be a list of content blocks."""
        client = _mock_acp_client(
            stream_events=[
                StreamEvent(event_type="content_delta", data={"content": "ok"}),
                StreamEvent(event_type="result", data={}),
            ]
        )

        session = _make_session(project_path="/tmp/test")
        with patch(
            "gobby.servers.gemini_cli_chat_session.GeminiACPClient",
            return_value=client,
        ):
            await session.start()
            events = [e async for e in session.send_message([{"type": "text", "text": "hello"}])]

        text_events = [e for e in events if isinstance(e, TextChunk)]
        assert len(text_events) == 1

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
        client = _mock_acp_client()
        session = _make_session(project_path="/tmp/test")

        with patch(
            "gobby.servers.gemini_cli_chat_session.GeminiACPClient",
            return_value=client,
        ):
            await session.start()
            assert session.is_connected
            await session.stop()

        assert not session.is_connected
        client.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_drain_is_noop(self) -> None:
        """drain_pending_response is a no-op for Gemini."""
        session = _make_session()
        await session.drain_pending_response()  # Should not raise

    @pytest.mark.asyncio
    async def test_interrupt_is_noop(self) -> None:
        """interrupt is a no-op for Gemini ACP."""
        session = _make_session()
        await session.interrupt()  # Should not raise


# ---------------------------------------------------------------------------
# Model switching
# ---------------------------------------------------------------------------


class TestModelSwitching:
    @pytest.mark.asyncio
    async def test_switch_model_stores_value(self) -> None:
        client = _mock_acp_client()
        session = _make_session(project_path="/tmp/test")

        with patch(
            "gobby.servers.gemini_cli_chat_session.GeminiACPClient",
            return_value=client,
        ):
            await session.start(model="gemini-2.5-pro")
            assert session.model == "gemini-2.5-pro"
            await session.switch_model("gemini-2.5-flash")
            assert session.model == "gemini-2.5-flash"
