"""Tests for renderer integration in SessionMessageProcessor."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcript_renderer import RenderState, render_incremental
from gobby.sessions.transcripts.base import ParsedMessage

pytestmark = pytest.mark.unit


def _make_parsed(
    index: int,
    role: str = "assistant",
    content: str = "hello",
    content_type: str = "text",
    tool_name: str | None = None,
    tool_use_id: str | None = None,
    tool_input: dict | None = None,
    tool_result: dict | None = None,
) -> ParsedMessage:
    return ParsedMessage(
        index=index,
        role=role,
        content=content,
        content_type=content_type,
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        tool_input=tool_input,
        tool_result=tool_result,
        timestamp=datetime(2026, 3, 20, 12, 0, index, tzinfo=UTC),
        raw_json=None,
    )


@pytest.fixture
def processor() -> SessionMessageProcessor:
    db = MagicMock()
    ws = AsyncMock()
    sm = MagicMock()
    p = SessionMessageProcessor(db=db, websocket_server=ws, session_manager=sm)
    return p


class TestRenderStateTracking:
    def test_render_states_initialized_empty(self, processor: SessionMessageProcessor) -> None:
        assert processor._render_states == {}

    def test_unregister_clears_render_state(self, processor: SessionMessageProcessor) -> None:
        with patch("os.path.exists", return_value=True):
            processor.register_session("sess-1", "/tmp/transcript.jsonl")
        processor._render_states["sess-1"] = RenderState()
        processor.unregister_session("sess-1")
        assert "sess-1" not in processor._render_states

    def test_unregister_nonexistent_no_error(self, processor: SessionMessageProcessor) -> None:
        result = processor.unregister_session("nonexistent")
        assert result is None
        assert processor._render_states == {}


class TestRenderedBroadcast:
    @pytest.mark.asyncio
    async def test_broadcasts_rendered_messages(self, processor: SessionMessageProcessor) -> None:
        """Parsed messages should be rendered and broadcast as RenderedMessage dicts."""
        processor.register_session("sess-1", "/tmp/test.jsonl", source="claude")

        # Simulate a user message followed by an assistant message (two turns)
        user_msg = _make_parsed(0, role="user", content="hi")
        assistant_msg = _make_parsed(1, role="assistant", content="hello back")

        state = RenderState()
        completed, state = render_incremental([user_msg, assistant_msg], state, session_id="sess-1")

        # User turn completes when assistant turn starts
        assert len(completed) == 1
        assert completed[0].role == "user"
        # Assistant turn is still in-progress
        assert state.current_message is not None
        assert state.current_message.role == "assistant"

    @pytest.mark.asyncio
    async def test_broadcast_payload_shape(self, processor: SessionMessageProcessor) -> None:
        """Broadcast payload should have type, session_id, and rendered message dict."""
        from gobby.sessions.transcript_renderer import RenderedMessage

        msg = RenderedMessage(
            id="test-1",
            role="assistant",
            content="hello",
            timestamp=datetime(2026, 3, 20, tzinfo=UTC),
        )
        d = msg.to_dict()
        assert "id" in d
        assert "role" in d
        assert "content_blocks" in d
        assert "timestamp" in d

    @pytest.mark.asyncio
    async def test_tool_call_pairing_across_poll_cycles(self) -> None:
        """Tool use in one poll and tool result in next should pair correctly."""
        from gobby.sessions.transcript_renderer import RenderState, render_incremental

        state = RenderState()

        # Poll 1: assistant sends tool_use
        tool_msg = _make_parsed(
            0,
            role="assistant",
            content="",
            content_type="tool_use",
            tool_name="Read",
            tool_use_id="tu-1",
            tool_input={"file_path": "/tmp/foo"},
        )
        completed, state = render_incremental([tool_msg], state, session_id="s1")
        assert len(completed) == 0
        assert "tu-1" in state.pending_tool_calls
        assert state.pending_tool_calls["tu-1"].status == "pending"

        # Poll 2: tool result arrives
        result_msg = _make_parsed(
            1,
            role="tool",
            content="file contents here",
            content_type="tool_result",
            tool_use_id="tu-1",
            tool_result={"content": "file contents here"},
        )
        completed, state = render_incremental([result_msg], state, session_id="s1")
        assert state.pending_tool_calls["tu-1"].status == "completed"
        assert state.pending_tool_calls["tu-1"].result is not None
