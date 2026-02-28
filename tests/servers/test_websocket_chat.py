"""Tests for WebSocket chat message handlers (ChatMixin)."""

import logging
from unittest.mock import MagicMock

import pytest

from gobby.servers.websocket.chat import ChatMixin

pytestmark = pytest.mark.unit


class MockWebSocket:
    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)


class ChatMixinHost(ChatMixin):
    """Minimal host class providing attributes ChatMixin expects."""

    def __init__(self) -> None:
        self.clients: dict = {}
        self._chat_sessions: dict = {}
        self._active_chat_tasks: dict = {}

    async def _send_error(
        self,
        websocket: object,
        message: str,
        request_id: str | None = None,
        code: str = "ERROR",
    ) -> None:
        pass


@pytest.fixture
def host() -> ChatMixinHost:
    return ChatMixinHost()


@pytest.fixture
def websocket() -> MockWebSocket:
    return MockWebSocket()


class TestHandleAskUserResponse:
    """Tests for _handle_ask_user_response handler."""

    @pytest.mark.asyncio
    async def test_calls_provide_answer_on_session(
        self, host: ChatMixinHost, websocket: MockWebSocket
    ) -> None:
        """Handler should look up session and call provide_answer with answers."""
        session = MagicMock()
        session.has_pending_question = True
        host._chat_sessions["conv-123"] = session

        data = {
            "type": "ask_user_response",
            "conversation_id": "conv-123",
            "tool_call_id": "tool-abc",
            "answers": {"Which auth?": "OAuth"},
        }

        await host._handle_ask_user_response(websocket, data)

        session.provide_answer.assert_called_once_with({"Which auth?": "OAuth"})

    @pytest.mark.asyncio
    async def test_missing_conversation_id_logs_warning(
        self, host: ChatMixinHost, websocket: MockWebSocket, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handler should log warning if conversation_id not found in sessions."""
        data = {
            "type": "ask_user_response",
            "conversation_id": "nonexistent",
            "answers": {"Q": "A"},
        }

        with caplog.at_level(logging.WARNING):
            await host._handle_ask_user_response(websocket, data)

        assert "nonexistent" in caplog.text

    @pytest.mark.asyncio
    async def test_no_pending_question_logs_warning(
        self, host: ChatMixinHost, websocket: MockWebSocket, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Handler should log warning if session has no pending question."""
        session = MagicMock()
        session.has_pending_question = False
        host._chat_sessions["conv-456"] = session

        data = {
            "type": "ask_user_response",
            "conversation_id": "conv-456",
            "answers": {"Q": "A"},
        }

        with caplog.at_level(logging.WARNING):
            await host._handle_ask_user_response(websocket, data)

        assert "no pending question" in caplog.text.lower() or "conv-456" in caplog.text
        session.provide_answer.assert_not_called()
