"""Tests for WebSocket chat message handlers (ChatMixin)."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.hooks.events import HookEventType
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
        self,
        host: ChatMixinHost,
        websocket: MockWebSocket,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Handler should look up session and call provide_answer with answers."""
        session = MagicMock()
        session.has_pending_question = True
        session.provide_answer.return_value = True
        host._chat_sessions["conv-123"] = session
        fire_lifecycle = AsyncMock(return_value=None)
        monkeypatch.setattr(host, "_fire_lifecycle", fire_lifecycle)

        data = {
            "type": "ask_user_response",
            "conversation_id": "conv-123",
            "tool_call_id": "tool-abc",
            "answers": {"Which auth?": "OAuth"},
        }

        await host._handle_ask_user_response(websocket, data)

        session.provide_answer.assert_called_once_with("tool-abc", {"Which auth?": "OAuth"})
        assert session.provide_answer.call_count == 1
        assert session.provide_answer.call_args is not None
        fire_lifecycle.assert_awaited_once_with(
            "conv-123",
            HookEventType.NOTIFICATION,
            {
                "tool_use_id": "tool-abc",
                "_gobby_wait_resolution": "resumed",
            },
        )

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


class TestHandleToolApprovalResponse:
    @pytest.mark.asyncio
    async def test_exact_managed_response_resumes_lifecycle(
        self,
        host: ChatMixinHost,
        websocket: MockWebSocket,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = MagicMock()
        session.provide_approval.return_value = True
        host._chat_sessions["conv-approval"] = session
        fire_lifecycle = AsyncMock(return_value=None)
        monkeypatch.setattr(host, "_fire_lifecycle", fire_lifecycle)

        await host._handle_tool_approval_response(
            websocket,
            {
                "conversation_id": "conv-approval",
                "tool_call_id": "approval-1",
                "decision": "reject",
            },
        )

        fire_lifecycle.assert_awaited_once_with(
            "conv-approval",
            HookEventType.NOTIFICATION,
            {
                "tool_use_id": "approval-1",
                "decision": "reject",
                "_gobby_wait_resolution": "resumed",
            },
        )

    @pytest.mark.asyncio
    async def test_mismatched_managed_response_does_not_resolve_lifecycle(
        self,
        host: ChatMixinHost,
        websocket: MockWebSocket,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = MagicMock()
        session.provide_approval.return_value = False
        session.has_pending_approval = True
        host._chat_sessions["conv-approval"] = session
        fire_lifecycle = AsyncMock(return_value=None)
        monkeypatch.setattr(host, "_fire_lifecycle", fire_lifecycle)

        await host._handle_tool_approval_response(
            websocket,
            {
                "conversation_id": "conv-approval",
                "tool_call_id": "wrong-id",
                "decision": "approve",
            },
        )

        fire_lifecycle.assert_not_awaited()
