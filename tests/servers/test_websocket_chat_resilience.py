"""Tests for chat messaging resilience fixes (_safe_send, error classification, etc.)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError

from gobby.servers.websocket.chat._messaging import ChatMessagingMixin

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockWebSocket:
    """WebSocket mock that can optionally raise on send."""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.sent_messages: list[str] = []
        self._fail_after = fail_after
        self._send_count = 0

    async def send(self, message: str) -> None:
        self._send_count += 1
        if self._fail_after is not None and self._send_count > self._fail_after:
            raise ConnectionClosedError(None, None)
        self.sent_messages.append(message)


class DisconnectedWebSocket:
    """WebSocket that always raises ConnectionClosed on send."""

    async def send(self, message: str) -> None:
        raise ConnectionClosedError(None, None)


class _FakeSession:
    """Minimal ChatSession stand-in."""

    def __init__(self) -> None:
        self.db_session_id = "db-session-123"
        self.message_index = 0
        self.seq_num = 1
        self.model = "claude-sonnet-4-6"
        self._pending_agent_name = None
        self._plan_approval_completed = False
        self._tool_approval_callback: Any = None
        self._accumulated_output_tokens = 0
        self._accumulated_cost_usd = 0.0
        self._last_model = None

    async def send_message(self, content: Any) -> Any:  # noqa: ANN401
        """Yield nothing — tests inject events directly."""
        return
        yield  # make it an async generator


class ChatMixinHost(ChatMessagingMixin):
    """Minimal host providing attributes ChatMessagingMixin expects."""

    def __init__(self) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_modes: dict[str, str] = {}
        self._pending_worktree_paths: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}
        self.message_manager: Any = None
        self.session_manager: Any = None

    async def _send_error(
        self, websocket: object, message: str, request_id: str | None = None, code: str = "ERROR"
    ) -> None:
        pass

    async def broadcast_session_event(self, event: str, session_id: str, **kwargs: Any) -> None:
        pass

    async def _fire_lifecycle(
        self, conversation_id: str, event_type: Any, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        return None

    async def _cancel_active_chat(self, conversation_id: str) -> None:
        pass

    async def _evaluate_blocking_webhooks(self, event: Any) -> dict[str, Any] | None:
        return None

    async def _create_chat_session(
        self,
        conversation_id: str,
        model: str | None = None,
        project_id: str | None = None,
        resume_session_id: str | None = None,
    ) -> Any:
        session = _FakeSession()
        self._chat_sessions[conversation_id] = session
        return session


@pytest.fixture
def host() -> ChatMixinHost:
    return ChatMixinHost()


# ---------------------------------------------------------------------------
# 1. _safe_send tests
# ---------------------------------------------------------------------------


class TestSafeSend:
    """Tests for the _safe_send disconnection detection helper."""

    @pytest.mark.asyncio
    async def test_safe_send_catches_connection_closed(self, host: ChatMixinHost) -> None:
        """_safe_send should catch ConnectionClosed and break the loop."""
        ws = DisconnectedWebSocket()
        host.clients[ws] = {"conversation_id": "conv-1"}

        # We need to invoke _stream_chat_response with events that will trigger sends.
        # Instead, test the mechanism by creating a DoneEvent-only stream.
        from gobby.llm.claude_models import TextChunk

        events = [
            TextChunk(content="hello"),
            TextChunk(content=" world"),  # should NOT be sent
        ]

        session = _FakeSession()

        async def _fake_send_message(content: Any) -> Any:  # noqa: ANN401
            for e in events:
                yield e

        session.send_message = _fake_send_message
        host._chat_sessions["conv-1"] = session

        await host._stream_chat_response(ws, "conv-1", "test", None)

        # WebSocket was disconnected — no messages should have been sent
        # (DisconnectedWebSocket raises immediately)
        # The important thing: no unhandled exception

    @pytest.mark.asyncio
    async def test_safe_send_skips_after_disconnect(self, host: ChatMixinHost) -> None:
        """After first failure, subsequent _safe_send calls should be no-ops."""
        # WebSocket that fails after 1 successful send
        ws = MockWebSocket(fail_after=1)
        host.clients[ws] = {"conversation_id": "conv-2"}

        from gobby.llm.claude_models import TextChunk

        events = [
            TextChunk(content="first"),
            TextChunk(content="second"),  # send will fail
            TextChunk(content="third"),  # should be skipped
        ]

        session = _FakeSession()

        async def _fake_send_message(content: Any) -> Any:  # noqa: ANN401
            for e in events:
                yield e

        session.send_message = _fake_send_message
        host._chat_sessions["conv-2"] = session

        await host._stream_chat_response(ws, "conv-2", "test", None)

        # Only the first text chunk should succeed (session already exists, no session_info)
        assert len(ws.sent_messages) == 1
        parsed = json.loads(ws.sent_messages[0])
        assert parsed["type"] == "chat_stream"
        assert parsed["content"] == "first"

    @pytest.mark.asyncio
    async def test_done_event_persists_even_on_disconnect(self, host: ChatMixinHost) -> None:
        """DoneEvent should still persist text even if websocket send fails."""
        ws = DisconnectedWebSocket()
        host.clients[ws] = {"conversation_id": "conv-3"}

        from gobby.llm.claude_models import DoneEvent

        mock_msg_mgr = AsyncMock()
        host.message_manager = mock_msg_mgr

        session = _FakeSession()

        async def _fake_send_message(content: Any) -> Any:  # noqa: ANN401
            yield DoneEvent(tool_calls_count=0)

        session.send_message = _fake_send_message
        host._chat_sessions["conv-3"] = session

        # Should not raise despite websocket being disconnected
        await host._stream_chat_response(ws, "conv-3", "test", None)


# ---------------------------------------------------------------------------
# 2. _classify_chat_error tests
# ---------------------------------------------------------------------------


class TestClassifyChatError:
    """Tests for the _classify_chat_error static method."""

    def test_rate_limit(self) -> None:
        msg, code = ChatMessagingMixin._classify_chat_error(Exception("rate_limit exceeded"))
        assert code == "RATE_LIMITED"
        assert "rate limit" in msg.lower()

    def test_429_status(self) -> None:
        msg, code = ChatMessagingMixin._classify_chat_error(Exception("HTTP 429 Too Many Requests"))
        assert code == "RATE_LIMITED"

    def test_auth_error(self) -> None:
        msg, code = ChatMessagingMixin._classify_chat_error(Exception("401 Unauthorized"))
        assert code == "AUTH_ERROR"
        assert "authentication" in msg.lower()

    def test_forbidden(self) -> None:
        msg, code = ChatMessagingMixin._classify_chat_error(Exception("403 Forbidden"))
        assert code == "AUTH_ERROR"

    def test_api_key_error(self) -> None:
        msg, code = ChatMessagingMixin._classify_chat_error(Exception("Invalid api_key provided"))
        assert code == "AUTH_ERROR"

    def test_timeout(self) -> None:
        msg, code = ChatMessagingMixin._classify_chat_error(TimeoutError())
        assert code == "TIMEOUT"
        assert "timed out" in msg.lower()

    def test_timeout_in_message(self) -> None:
        msg, code = ChatMessagingMixin._classify_chat_error(Exception("request timeout after 30s"))
        assert code == "TIMEOUT"

    def test_connection_error(self) -> None:
        msg, code = ChatMessagingMixin._classify_chat_error(Exception("connection refused"))
        assert code == "CONNECTION_ERROR"

    def test_unknown_error_includes_type(self) -> None:
        msg, code = ChatMessagingMixin._classify_chat_error(ValueError("something weird"))
        assert code == "INTERNAL_ERROR"
        assert "ValueError" in msg


# ---------------------------------------------------------------------------
# 3. Persistence failure warning tests
# ---------------------------------------------------------------------------


class TestPersistFailureWarning:
    """Tests for chat_warning on persistence failures."""

    @pytest.mark.asyncio
    async def test_persist_message_failure_sends_warning(
        self, host: ChatMixinHost, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When _persist_message fails, a chat_warning should be sent to the client."""
        ws = MockWebSocket()
        host.clients[ws] = {"conversation_id": "conv-4"}

        from gobby.llm.claude_models import DoneEvent, TextChunk

        # Set up a message manager that will fail
        mock_msg_mgr = AsyncMock()
        mock_msg_mgr.store_messages.side_effect = RuntimeError("DB locked")
        host.message_manager = mock_msg_mgr

        session = _FakeSession()

        async def _fake_send_message(content: Any) -> Any:  # noqa: ANN401
            yield TextChunk(content="hello")
            yield DoneEvent(tool_calls_count=0)

        session.send_message = _fake_send_message
        host._chat_sessions["conv-4"] = session

        with caplog.at_level(logging.ERROR):
            await host._stream_chat_response(ws, "conv-4", "test", None)

        # Should have logged at ERROR level
        assert any(
            "Failed to persist" in r.message for r in caplog.records if r.levelno >= logging.ERROR
        )

        # Should have sent chat_warning to client
        warnings = [json.loads(m) for m in ws.sent_messages if "chat_warning" in m]
        assert len(warnings) >= 1
        assert warnings[0]["code"] == "PERSIST_FAILED"

    @pytest.mark.asyncio
    async def test_persist_tool_call_failure_sends_warning(
        self, host: ChatMixinHost, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When _persist_tool_call fails, a chat_warning should be sent."""
        ws = MockWebSocket()
        host.clients[ws] = {"conversation_id": "conv-5"}

        from gobby.llm.claude_models import DoneEvent, ToolCallEvent, ToolResultEvent

        mock_msg_mgr = AsyncMock()
        # First call succeeds (user message persist), subsequent calls fail
        call_count = 0

        async def _fail_on_tool(sid: str, msgs: list[Any]) -> None:
            nonlocal call_count
            call_count += 1
            if call_count > 1:  # First is user message, second is tool call
                raise RuntimeError("DB write failed")

        mock_msg_mgr.store_messages.side_effect = _fail_on_tool
        host.message_manager = mock_msg_mgr

        session = _FakeSession()

        async def _fake_send_message(content: Any) -> Any:  # noqa: ANN401
            yield ToolCallEvent(
                tool_call_id="tc-1",
                tool_name="test_tool",
                server_name="test",
                arguments={"key": "val"},
            )
            yield ToolResultEvent(
                tool_call_id="tc-1",
                result="ok",
                error=None,
                success=True,
            )
            yield DoneEvent(tool_calls_count=1)

        session.send_message = _fake_send_message
        host._chat_sessions["conv-5"] = session

        with caplog.at_level(logging.ERROR):
            await host._stream_chat_response(ws, "conv-5", "test", None)

        warnings = [json.loads(m) for m in ws.sent_messages if "chat_warning" in m]
        assert len(warnings) >= 1
        assert warnings[0]["warning"] == "Tool call may not be saved to history"


# ---------------------------------------------------------------------------
# 4. Orphaned tool result warning test
# ---------------------------------------------------------------------------


class TestOrphanedToolResult:
    """Tests for warning when ToolResult arrives without prior ToolCall."""

    @pytest.mark.asyncio
    async def test_orphaned_tool_result_logs_warning(
        self, host: ChatMixinHost, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ToolResultEvent without a prior ToolCallEvent should log a warning."""
        ws = MockWebSocket()
        host.clients[ws] = {"conversation_id": "conv-6"}

        from gobby.llm.claude_models import DoneEvent, ToolResultEvent

        session = _FakeSession()

        async def _fake_send_message(content: Any) -> Any:  # noqa: ANN401
            # Emit ToolResultEvent WITHOUT a preceding ToolCallEvent
            yield ToolResultEvent(
                tool_call_id="orphan-tc-1",
                result="some result",
                error=None,
                success=True,
            )
            yield DoneEvent(tool_calls_count=0)

        session.send_message = _fake_send_message
        host._chat_sessions["conv-6"] = session

        with caplog.at_level(logging.WARNING):
            await host._stream_chat_response(ws, "conv-6", "test", None)

        assert any(
            "arrived before ToolCallEvent" in r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )
