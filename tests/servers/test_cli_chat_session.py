"""Tests for CLIChatSession — Claude CLI-backed web chat session."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.llm.claude_models import DoneEvent, TextChunk, ThinkingEvent

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _mock_cli_imports(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the not-yet-existing claude_cli and stream_json_parser modules.

    Returns a dict with the mock module objects for assertions.
    """
    import sys
    import types

    # --- stream_json_parser stubs ---
    sjp = types.ModuleType("gobby.llm.stream_json_parser")

    class StreamEvent:
        pass

    class ContentBlockDelta(StreamEvent):
        def __init__(self, block_type: str, text: str) -> None:
            self.block_type = block_type
            self.text = text

    class ResultEvent(StreamEvent):
        def __init__(
            self,
            cost_usd: float | None = None,
            input_tokens: int | None = None,
            output_tokens: int | None = None,
        ) -> None:
            self.cost_usd = cost_usd
            self.input_tokens = input_tokens
            self.output_tokens = output_tokens

    class InitEvent(StreamEvent):
        pass

    class ErrorEvent(StreamEvent):
        pass

    sjp.StreamEvent = StreamEvent  # type: ignore[attr-defined]
    sjp.ContentBlockDelta = ContentBlockDelta  # type: ignore[attr-defined]
    sjp.ResultEvent = ResultEvent  # type: ignore[attr-defined]
    sjp.InitEvent = InitEvent  # type: ignore[attr-defined]
    sjp.ErrorEvent = ErrorEvent  # type: ignore[attr-defined]

    # --- claude_cli stubs ---
    cc = types.ModuleType("gobby.llm.claude_cli")

    class CLISession:
        async def start(self) -> None:
            pass

        async def send(self, text: str):  # type: ignore[override]
            # Must be overridden per-test via mock
            if False:  # pragma: no cover
                yield text

        async def interrupt(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    class ClaudeCLI:
        def session(self, **kwargs: Any) -> CLISession:
            return CLISession()

    cc.ClaudeCLI = ClaudeCLI  # type: ignore[attr-defined]
    cc.CLISession = CLISession  # type: ignore[attr-defined]

    # Inject into sys.modules BEFORE importing CLIChatSession
    sys.modules["gobby.llm.stream_json_parser"] = sjp
    sys.modules["gobby.llm.claude_cli"] = cc

    yield {
        "stream_json_parser": sjp,
        "claude_cli": cc,
        "ContentBlockDelta": ContentBlockDelta,
        "ResultEvent": ResultEvent,
        "CLISession": CLISession,
        "ClaudeCLI": ClaudeCLI,
    }

    # Cleanup — remove injected modules so other tests aren't affected
    sys.modules.pop("gobby.llm.stream_json_parser", None)
    sys.modules.pop("gobby.llm.claude_cli", None)
    # Also remove the cached CLIChatSession module so it re-imports cleanly
    sys.modules.pop("gobby.servers.cli_chat_session", None)


@pytest.fixture()
def mocks(_mock_cli_imports: dict[str, Any]) -> dict[str, Any]:
    """Return the mock modules/classes dict."""
    return _mock_cli_imports


def _import_session_class():
    """Import CLIChatSession after mocks are installed."""
    from gobby.servers.cli_chat_session import CLIChatSession

    return CLIChatSession


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCLIChatSessionProvider:
    """Test the provider class attribute."""

    def test_provider_is_claude(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        assert CLIChatSession.provider == "claude"

    def test_provider_on_instance(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")
        assert session.provider == "claude"


class TestCLIChatSessionStart:
    """Test session start lifecycle."""

    @pytest.mark.asyncio
    async def test_start_connects(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv", model="opus")

        assert not session.is_connected

        # Mock the CLI to return a mock CLISession
        mock_cli_session = AsyncMock()
        mock_cli_session.start = AsyncMock()
        session._cli = MagicMock()
        session._cli.session.return_value = mock_cli_session

        await session.start()

        assert session.is_connected
        session._cli.session.assert_called_once_with(
            session_id=None,
            model="opus",
            env_overrides={},
        )
        mock_cli_session.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_with_model_override(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv", model="sonnet")

        mock_cli_session = AsyncMock()
        mock_cli_session.start = AsyncMock()
        session._cli = MagicMock()
        session._cli.session.return_value = mock_cli_session

        await session.start(model="haiku")

        session._cli.session.assert_called_once_with(
            session_id=None,
            model="haiku",
            env_overrides={},
        )


class TestCLIChatSessionSendMessage:
    """Test send_message streaming."""

    @pytest.mark.asyncio
    async def test_send_message_text_chunks(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        ContentBlockDelta = mocks["ContentBlockDelta"]
        ResultEvent = mocks["ResultEvent"]

        session = CLIChatSession(conversation_id="test-conv")

        # Build a mock CLI session that yields stream events
        events = [
            ContentBlockDelta(block_type="text", text="Hello "),
            ContentBlockDelta(block_type="text", text="world"),
            ResultEvent(cost_usd=0.01, input_tokens=100, output_tokens=50),
        ]

        async def mock_send(text: str):
            for ev in events:
                yield ev

        mock_cli_session = MagicMock()
        mock_cli_session.send = mock_send
        session._cli_session = mock_cli_session
        session._connected = True

        collected: list[Any] = []
        async for chat_event in session.send_message("Hi"):
            collected.append(chat_event)

        assert len(collected) == 3
        assert isinstance(collected[0], TextChunk)
        assert collected[0].content == "Hello "
        assert isinstance(collected[1], TextChunk)
        assert collected[1].content == "world"
        assert isinstance(collected[2], DoneEvent)
        assert collected[2].cost_usd == 0.01

    @pytest.mark.asyncio
    async def test_send_message_thinking_events(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        ContentBlockDelta = mocks["ContentBlockDelta"]

        session = CLIChatSession(conversation_id="test-conv")

        events = [
            ContentBlockDelta(block_type="thinking", text="Let me think..."),
        ]

        async def mock_send(text: str):
            for ev in events:
                yield ev

        mock_cli_session = MagicMock()
        mock_cli_session.send = mock_send
        session._cli_session = mock_cli_session
        session._connected = True

        collected: list[Any] = []
        async for chat_event in session.send_message("Think about this"):
            collected.append(chat_event)

        assert len(collected) == 1
        assert isinstance(collected[0], ThinkingEvent)
        assert collected[0].content == "Let me think..."

    @pytest.mark.asyncio
    async def test_send_message_multimodal_content(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        ContentBlockDelta = mocks["ContentBlockDelta"]

        session = CLIChatSession(conversation_id="test-conv")

        sent_text: str | None = None

        async def mock_send(text: str):
            nonlocal sent_text
            sent_text = text
            yield ContentBlockDelta(block_type="text", text="ok")

        mock_cli_session = MagicMock()
        mock_cli_session.send = mock_send
        session._cli_session = mock_cli_session
        session._connected = True

        content_blocks = [
            {"type": "text", "text": "First part"},
            {"type": "image", "source": {"data": "..."}},
            {"type": "text", "text": "Second part"},
        ]

        collected: list[Any] = []
        async for chat_event in session.send_message(content_blocks):
            collected.append(chat_event)

        assert sent_text == "First part\nSecond part"

    @pytest.mark.asyncio
    async def test_send_message_not_connected_raises(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        with pytest.raises(RuntimeError, match="not connected"):
            async for _ in session.send_message("Hi"):
                pass  # pragma: no cover


class TestCLIChatSessionInterrupt:
    """Test interrupt."""

    @pytest.mark.asyncio
    async def test_interrupt_delegates(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        mock_cli_session = AsyncMock()
        session._cli_session = mock_cli_session

        await session.interrupt()
        mock_cli_session.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interrupt_no_session_is_safe(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        # Should not raise
        await session.interrupt()


class TestCLIChatSessionStop:
    """Test stop lifecycle."""

    @pytest.mark.asyncio
    async def test_stop_disconnects(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        mock_cli_session = AsyncMock()
        session._cli_session = mock_cli_session
        session._connected = True

        await session.stop()

        assert not session.is_connected
        assert session._cli_session is None
        mock_cli_session.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_handles_error(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        mock_cli_session = AsyncMock()
        mock_cli_session.stop.side_effect = RuntimeError("already dead")
        session._cli_session = mock_cli_session
        session._connected = True

        # Should not raise
        await session.stop()
        assert not session.is_connected

    @pytest.mark.asyncio
    async def test_stop_no_session_is_safe(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        # Should not raise
        await session.stop()


class TestCLIChatSessionProtocolConformance:
    """Test that all protocol attributes exist."""

    def test_has_all_protocol_attributes(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        # Identity attrs
        assert session.conversation_id == "test-conv"
        assert session.db_session_id is None
        assert session.seq_num is None
        assert session.project_id is None
        assert session.project_path is None
        assert session.message_index == 0
        assert session.chat_mode == "code"
        assert session.system_prompt_override is None
        assert session.resume_session_id is None
        assert isinstance(session.last_activity, datetime)

        # Lifecycle callbacks
        assert session._on_before_agent is None
        assert session._on_pre_tool is None
        assert session._on_post_tool is None
        assert session._on_pre_compact is None
        assert session._on_stop is None
        assert session._on_mode_changed is None
        assert session._on_plan_ready is None

        # Optional attrs
        assert session._tool_approval_config is None
        assert session._tool_approval_callback is None
        assert session._session_manager_ref is None
        assert session._on_mode_persist is None
        assert session._on_approved_tools_persist is None
        assert session._approved_tools == set()
        assert session._plan_file_path is None
        assert session._pending_agent_name is None
        assert session._plan_approval_completed is False
        assert session._context_window_overrides == {}
        assert session._accumulated_output_tokens == 0
        assert session._accumulated_cost_usd == 0.0
        assert session._message_manager_source_session_id is None
        assert session._needs_history_injection is False
        assert session._message_manager is None

    def test_pending_states_always_false(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        assert session.has_pending_question is False
        assert session.has_pending_approval is False
        assert session.has_pending_plan is False

    def test_interaction_stubs_are_noop(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        # These should all be no-ops and not raise
        session.provide_answer({"q1": "a1"})
        session.provide_approval("approve")
        session.provide_plan_decision("approve")
        session.approve_plan()
        session.set_plan_feedback("looks good")

    @pytest.mark.asyncio
    async def test_switch_model(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv", model="opus")

        assert session.model == "opus"
        await session.switch_model("sonnet")
        assert session.model == "sonnet"

    def test_set_chat_mode(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        assert session.chat_mode == "code"
        session.set_chat_mode("plan")
        assert session.chat_mode == "plan"

    @pytest.mark.asyncio
    async def test_drain_pending_response_is_noop(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        # Should not raise
        await session.drain_pending_response()

    @pytest.mark.asyncio
    async def test_sync_sdk_permission_mode_is_noop(self, mocks: dict[str, Any]) -> None:
        CLIChatSession = _import_session_class()
        session = CLIChatSession(conversation_id="test-conv")

        # Should not raise
        await session.sync_sdk_permission_mode()
