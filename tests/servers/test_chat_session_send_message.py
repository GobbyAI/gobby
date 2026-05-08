"""Tests for ChatSession send_message and related client lifecycle methods."""

from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from gobby.llm.claude_models import (
    DoneEvent,
    TextChunk,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from gobby.servers.chat_session import ChatSession

pytestmark = pytest.mark.unit


@pytest.fixture
def session() -> ChatSession:
    sess = ChatSession(conversation_id="test-conv-123")
    sess._client = AsyncMock(spec=ClaudeSDKClient)
    sess._connected = True
    return sess


class TestChatSessionSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_not_connected(self) -> None:
        """Test send_message raises RuntimeError if disconnected."""
        sess = ChatSession(conversation_id="test-val")
        with pytest.raises(RuntimeError, match="ChatSession not connected"):
            # Can't use async for on an exception directly, so we just trigger it
            # by getting the generator
            gen = sess.send_message("hello")
            await anext(gen)

    @pytest.mark.asyncio
    async def test_send_message_plain_string(self, session: ChatSession) -> None:
        """Test send_message correctly formats a plain string input and yields text."""
        # Setup mock receive_response to yield a TextBlock then Done
        session._client.receive_response.return_value = TestChatSessionSendMessage._mock_stream(
            [
                AssistantMessage(
                    content=[TextBlock(text="Hello world")],
                    model="claude-3-opus-20240229",
                ),
                ResultMessage(
                    subtype="result",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk-123",
                    result="Hello world",
                ),
            ]
        )

        events = []
        async for event in session.send_message("Hi!"):
            events.append(event)

        session._client.query.assert_called_once_with("Hi!")
        assert len(events) == 2
        assert isinstance(events[0], TextChunk)
        assert events[0].content == "Hello world"
        assert isinstance(events[1], DoneEvent)
        assert events[1].sdk_session_id == "sdk-123"

    @pytest.mark.asyncio
    async def test_send_message_handles_list_content(self, session: ChatSession) -> None:
        """Test list content is reformatted for exact SDK input mapping."""
        session._client.receive_response.return_value = TestChatSessionSendMessage._mock_stream(
            [
                ResultMessage(
                    subtype="result",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk-123",
                    result="fallback",
                )
            ]
        )

        # Test content list
        content = [{"type": "text", "text": "Hi"}]
        async for _ in session.send_message(content):
            pass

        # Ensure it streamed the content properly mapped
        assert session._client.query.call_count == 1
        call_arg = session._client.query.call_args[0][0]
        # Should be an async iterator
        assert hasattr(call_arg, "__anext__")

        # Extract the yielded item
        items = []
        async for item in call_arg:
            items.append(item)

        assert len(items) == 1
        assert items[0] == {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

    @pytest.mark.asyncio
    async def test_send_message_parses_usage_streamevent(self, session: ChatSession) -> None:
        """Test stream event usage parsing (the message_start wrapper)."""
        session._client.receive_response.return_value = TestChatSessionSendMessage._mock_stream(
            [
                StreamEvent(
                    uuid="evt-1",
                    session_id="sdk",
                    event={
                        "type": "message_start",
                        "message": {
                            "usage": {
                                "input_tokens": 100,
                                "cache_read_input_tokens": 50,
                                "cache_creation_input_tokens": 10,
                            }
                        },
                    },
                ),
                ResultMessage(
                    subtype="result",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk",
                    result="Hello",
                    usage={"output_tokens": 20},
                ),
            ]
        )

        events = []
        async for ev in session.send_message("test"):
            events.append(ev)

        assert len(events) == 2
        done = events[1]
        assert getattr(done, "input_tokens", None) == 100
        assert getattr(done, "cache_read_input_tokens", None) == 50
        assert getattr(done, "cache_creation_input_tokens", None) == 10
        assert getattr(done, "total_input_tokens", None) == 160  # 100+50+10
        assert getattr(done, "output_tokens", None) == 20

    @pytest.mark.asyncio
    async def test_send_message_handles_tools(self, session: ChatSession) -> None:
        """Test parsing of tool uses and tool results."""
        session._client.receive_response.return_value = TestChatSessionSendMessage._mock_stream(
            [
                AssistantMessage(
                    content=[ToolUseBlock(id="tu_1", name="mcp__gobby__read", input={"path": "a"})],
                    model="test",
                ),
                UserMessage(
                    content=[ToolResultBlock(tool_use_id="tu_1", content="ok", is_error=False)],
                ),
                ResultMessage(
                    subtype="result",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk",
                    result="",
                ),
            ]
        )

        events = []
        async for ev in session.send_message("test"):
            events.append(ev)

        # 1. ToolCallEvent
        assert isinstance(events[0], ToolCallEvent)
        assert events[0].server_name == "gobby"
        assert events[0].tool_name == "mcp__gobby__read"
        assert events[0].arguments == {"path": "a"}

        # 2. ToolResultEvent
        assert isinstance(events[1], ToolResultEvent)
        assert events[1].tool_call_id == "tu_1"
        assert events[1].success is True
        assert events[1].result == "ok"

        # 3. DoneEvent
        assert isinstance(events[2], DoneEvent)
        assert events[2].tool_calls_count == 1

    @pytest.mark.asyncio
    async def test_send_message_thinking_block(self, session: ChatSession) -> None:
        session._client.receive_response.return_value = TestChatSessionSendMessage._mock_stream(
            [
                AssistantMessage(
                    content=[ThinkingBlock(thinking="hmm", signature="sig")],
                    model="claude-3-7",
                ),
                ResultMessage(
                    subtype="result",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="s",
                    result="res",
                ),
            ]
        )

        events = []
        async for ev in session.send_message("x"):
            events.append(ev)

        assert isinstance(events[0], ThinkingEvent)
        assert events[0].content == "hmm"

    @pytest.mark.asyncio
    async def test_send_message_handles_exception(self, session: ChatSession) -> None:
        # Mock receive_response to throw exception group
        async def failing_stream():
            raise ExceptionGroup("test", [ValueError("some issue")])
            yield  # pragma: no cover - required to make this an async generator

        session._client.receive_response.return_value = failing_stream()

        events = []
        async for ev in session.send_message("bad"):
            events.append(ev)

        # Expect a TextChunk with the error, then a DoneEvent
        assert len(events) == 2
        assert isinstance(events[0], TextChunk)
        assert "Generation failed:" in events[0].content
        assert isinstance(events[1], DoneEvent)

    @pytest.mark.asyncio
    async def test_lifecycle_methods(self, session: ChatSession) -> None:
        """Test interrupt, drain, and stop directly."""
        # Interrupt
        await session.interrupt()
        session._client.interrupt.assert_awaited_once()

        # Drain
        async def dummy_drain():
            yield 1
            yield 2

        session._client.receive_response.return_value = dummy_drain()
        await session.drain_pending_response()
        # Should finish successfully

        # Stop — save client ref before stop() clears it
        client_mock = session._client
        await session.stop()
        client_mock.disconnect.assert_awaited_once()
        assert not session._connected

    @pytest.mark.asyncio
    async def test_switch_model(self, session: ChatSession) -> None:
        await session.switch_model("new-model")
        session._client.set_model.assert_awaited_once_with("new-model")
        assert session.model == "new-model"

    @pytest.mark.asyncio
    async def test_send_message_reconnects_when_reasoning_effort_changes(
        self, session: ChatSession
    ) -> None:
        session.reasoning_effort = "high"
        session._active_reasoning_effort = None
        session._model = "sonnet"
        session.sdk_session_id = "sdk-123"
        session._reconnect_for_reasoning_effort_change = AsyncMock()
        session._client.receive_response.return_value = TestChatSessionSendMessage._mock_stream(
            [
                ResultMessage(
                    subtype="result",
                    duration_ms=0,
                    duration_api_ms=0,
                    is_error=False,
                    num_turns=1,
                    session_id="sdk-123",
                    result="ok",
                ),
            ]
        )

        async for _ in session.send_message("Hi!"):
            pass

        session._reconnect_for_reasoning_effort_change.assert_awaited_once()
        assert session._reconnect_for_reasoning_effort_change.await_count == 1
        assert session._reconnect_for_reasoning_effort_change.await_args is not None

    @staticmethod
    async def _mock_stream(items):
        """Helper to yield items as an async generator."""
        for item in items:
            yield item
