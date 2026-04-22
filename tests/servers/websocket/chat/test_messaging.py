"""Tests for WebSocket ChatMessagingMixin."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from gobby.hooks.events import HookEventType
from gobby.llm.claude_models import (
    DoneEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.websocket.chat._messaging import ChatMessagingMixin

pytestmark = pytest.mark.unit


class DummyMessagingMixin(ChatMessagingMixin):
    def __init__(self) -> None:
        self.clients: dict = {}
        self._chat_sessions: dict = {}
        self._active_chat_tasks: dict = {}
        self._pending_modes: dict = {}
        self._pending_worktree_paths: dict = {}
        self._pending_agents: dict = {}
        self.session_manager = None
        self.inter_session_msg_manager = None

    async def _send_error(
        self, ws: object, msg: str, request_id: str | None = None, code: str = "ERROR"
    ) -> None:
        await ws.send(json.dumps({"error": msg}))  # type: ignore[union-attr]

    async def _cancel_active_chat(self, cid: str) -> None:
        pass

    async def _create_chat_session(
        self,
        cid: str,
        model: str | None = None,
        project_id: str | None = None,
        resume_session_id: str | None = None,
        provider: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncMock:
        sess = AsyncMock()
        sess.db_session_id = "db-id"
        sess.model = "opus"
        self._chat_sessions[cid] = sess
        return sess

    async def broadcast_session_event(self, event: str, sid: str, **kwargs: object) -> None:
        pass


@pytest.fixture
def mixin() -> DummyMessagingMixin:
    return DummyMessagingMixin()


@pytest.fixture
def ws() -> AsyncMock:
    return AsyncMock()


class TestClassifyChatError:
    def test_classify(self, mixin: DummyMessagingMixin):
        msg, code = mixin._classify_chat_error(ValueError("429 rate_limit exceeded"))
        assert code == "RATE_LIMITED"

        msg, code = mixin._classify_chat_error(RuntimeError("auth failed 401"))
        assert code == "AUTH_ERROR"

        msg, code = mixin._classify_chat_error(TimeoutError("oops"))
        assert code == "TIMEOUT"

        msg, code = mixin._classify_chat_error(ConnectionError("lost connection"))
        assert code == "CONNECTION_ERROR"

        msg, code = mixin._classify_chat_error(RuntimeError("unknown issue"))
        assert code == "INTERNAL_ERROR"


class TestInjectPendingMessages:
    def test_inject_wrong_event(self, mixin: DummyMessagingMixin):
        assert mixin._inject_pending_messages("1", HookEventType.SESSION_START) is None

    def test_inject_no_manager(self, mixin: DummyMessagingMixin):
        # Already set to None in Dummy init
        assert mixin._inject_pending_messages("1", HookEventType.BEFORE_AGENT) is None

    def test_inject_success(self, mixin: DummyMessagingMixin):
        mixin.inter_session_msg_manager = MagicMock()

        msg1 = MagicMock()
        msg1.id = "1"
        msg1.message_type = "web_chat"
        msg1.from_session = "1234567890"
        msg1.content = "hello"

        msg2 = MagicMock()
        msg2.id = "2"
        msg2.message_type = "p2p"
        msg2.priority = "urgent"
        msg2.from_session = None
        msg2.content = "help me"

        mixin.inter_session_msg_manager.get_undelivered_messages.return_value = [msg1, msg2]

        res = mixin._inject_pending_messages("sid", HookEventType.BEFORE_AGENT)

        assert res is not None
        assert "Pending messages from web chat user" in res
        assert "- Session 12345678: hello" in res
        assert "Pending P2P messages from other sessions" in res
        assert "- [URGENT] help me" in res

        mixin.inter_session_msg_manager.mark_delivered.assert_any_call("1")
        mixin.inter_session_msg_manager.mark_delivered.assert_any_call("2")


class TestHandleChatMessage:
    @pytest.mark.asyncio
    async def test_no_content(self, mixin: DummyMessagingMixin, ws: AsyncMock):
        await mixin._handle_chat_message(ws, {"content": ""})
        ws.send.assert_called_once()
        assert "Missing or invalid 'content' field" in ws.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_unregistered_client(self, mixin: DummyMessagingMixin, ws: AsyncMock):
        # mixin.clients is empty
        await mixin._handle_chat_message(ws, {"content": "hi"})
        # Should return silently after warning log
        assert not ws.send.called

    @pytest.mark.asyncio
    async def test_success_dispatch(self, mixin: DummyMessagingMixin, ws: AsyncMock):
        mixin.clients[ws] = {"connected": True}

        with patch.object(mixin, "_stream_chat_response", new_callable=AsyncMock) as mock_stream:
            await mixin._handle_chat_message(ws, {"content": "hi", "conversation_id": "c1"})

            assert mixin.clients[ws]["conversation_id"] == "c1"
            assert "c1" in mixin._active_chat_tasks

            # Await the background task so the mock actually executes
            await mixin._active_chat_tasks["c1"]

            mock_stream.assert_awaited_once()
            call_args = mock_stream.call_args
            assert call_args[0][0] is ws
            assert call_args[0][1] == "c1"
            assert call_args[0][2] == "hi"
            assert call_args[0][3] is None  # model


class TestStreamChatResponse:
    @pytest.mark.asyncio
    async def test_startup_error_exposes_debug_detail(
        self,
        mixin: DummyMessagingMixin,
        ws: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mixin.clients[ws] = {"conversation_id": "c1"}

        with patch.object(
            mixin,
            "_create_chat_session",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with caplog.at_level("DEBUG", logger="gobby.servers.websocket.chat._messaging"):
                await mixin._stream_chat_response(ws, "c1", "hi", None)

        messages = [json.loads(call[0][0]) for call in ws.send.call_args_list]
        chat_error = next(msg for msg in messages if msg.get("type") == "chat_error")

        assert chat_error["error"] == "Failed to start chat session. Please try again."
        assert chat_error["error_detail"] == "RuntimeError: boom"
        assert any(
            record.levelname == "ERROR"
            and "Failed to start chat session for conversation c1" in record.message
            and record.exc_info
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_stream_model_switch(self, mixin: DummyMessagingMixin, ws: AsyncMock):
        mixin.clients[ws] = {"conversation_id": "c1"}
        session = AsyncMock()
        session.model = "opus"
        session.db_session_id = "db-id"
        mixin._chat_sessions["c1"] = session
        mixin.session_manager = MagicMock()

        async def dummy_generator(text):
            yield DoneEvent(
                sdk_session_id="sdk", input_tokens=10, output_tokens=5, tool_calls_count=0
            )

        # send_message must return an async generator directly (not a coroutine)
        session.send_message = lambda content: dummy_generator(content)

        await mixin._stream_chat_response(ws, "c1", "hi", "sonnet")

        session.switch_model.assert_awaited_once_with("sonnet")
        mixin.session_manager.update_model.assert_called_once_with("db-id", "sonnet")
        # Validate model switch message sent
        messages = [call[0][0] for call in ws.send.call_args_list]
        assert any("model_switched" in msg and "sonnet" in msg for msg in messages)

    @pytest.mark.asyncio
    async def test_stream_events(self, mixin: DummyMessagingMixin, ws: AsyncMock):
        mixin.clients[ws] = {"conversation_id": "c1"}
        session = AsyncMock()
        mixin._chat_sessions["c1"] = session

        async def mock_stream(content):
            yield ThinkingEvent(content="hmm")
            yield TextChunk(content="text block")
            yield ToolCallEvent(
                tool_call_id="call1", tool_name="read", server_name="srv", arguments={"p": 1}
            )
            yield ToolResultEvent(tool_call_id="call1", result="ok", success=True)
            yield DoneEvent(
                sdk_session_id="sdk", input_tokens=10, output_tokens=5, tool_calls_count=1
            )

        # send_message must return an async generator directly (not a coroutine)
        session.send_message = lambda content: mock_stream(content)

        await mixin._stream_chat_response(ws, "c1", "hi", None)

        msgs = []
        for call in ws.send.call_args_list:
            msgs.append(json.loads(call[0][0]))

        types = [m.get("type") for m in msgs]
        assert "chat_thinking" in types
        assert "chat_stream" in types
        assert "tool_status" in types

        # Web-chat sessions stay keyed by DB/UI session identity; sdk_session_id
        # is metadata only and must not mutate the in-memory/frontend key.
        assert "c1" in mixin._chat_sessions
        assert "sdk" not in mixin._chat_sessions

    @pytest.mark.asyncio
    async def test_stream_cancellation_safely(self, mixin: DummyMessagingMixin, ws: AsyncMock):
        mixin.clients[ws] = {"conversation_id": "c1"}
        session = AsyncMock()
        mixin._chat_sessions["c1"] = session

        async def canceling_stream(content):
            raise asyncio.CancelledError()
            yield  # required to make this an async generator

        # send_message must return an async generator directly (not a coroutine)
        session.send_message = lambda content: canceling_stream(content)

        await mixin._stream_chat_response(ws, "c1", "hi", None)

        msgs = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        done_msg = [m for m in msgs if m.get("done") is True]
        assert len(done_msg) == 1
        assert done_msg[0]["interrupted"] is True

    @pytest.mark.asyncio
    async def test_stream_client_disconnect(self, mixin: DummyMessagingMixin, ws: AsyncMock):
        mixin.clients[ws] = {"conversation_id": "c1"}
        session = AsyncMock()
        mixin._chat_sessions["c1"] = session

        async def dummy_stream(content):
            yield ThinkingEvent(content="hmm")

        # send_message must return an async generator directly (not a coroutine)
        session.send_message = lambda content: dummy_stream(content)

        ws.send.side_effect = ConnectionClosed(None, None)

        # Should catch gracefully and not propagate
        await mixin._stream_chat_response(ws, "c1", "hi", None)
