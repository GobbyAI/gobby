"""Tests for CLIChatSession — Claude CLI-backed web chat sessions.

Covers:
- start() spawns ClaudeCLI session
- send_message() translates StreamEvents to ChatEvents
- interrupt() delegates to CLISession
- stop() delegates to CLISession
- provider attribute is "claude"
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gobby.servers.cli_chat_session import CLIChatSession

pytestmark = pytest.mark.unit


def _make_session(**overrides: Any) -> CLIChatSession:
    """Create a CLIChatSession with test defaults."""
    defaults: dict[str, Any] = {
        "conversation_id": "test-conv-456",
    }
    defaults.update(overrides)
    return CLIChatSession(**defaults)


class TestProviderAttribute:
    """Tests for CLIChatSession.provider."""

    def test_provider_is_claude(self) -> None:
        """CLIChatSession.provider should be 'claude'."""
        session = _make_session()
        assert session.provider == "claude"


class TestStart:
    """Tests for CLIChatSession.start()."""

    @pytest.mark.asyncio
    async def test_start_spawns_cli_session(self) -> None:
        """start() should spawn a ClaudeCLI session."""
        session = _make_session()

        with patch.object(session, "_spawn_cli_session", new_callable=AsyncMock) as mock_spawn:
            mock_spawn.return_value = MagicMock()
            await session.start()

            mock_spawn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_sets_internal_session(self) -> None:
        """start() should set the internal CLI session reference."""
        session = _make_session()

        mock_cli = MagicMock()
        with patch.object(session, "_spawn_cli_session", new_callable=AsyncMock) as mock_spawn:
            mock_spawn.return_value = mock_cli
            await session.start()

        # After start, the session should have an internal CLI reference
        assert session._cli_session is not None


class TestSendMessage:
    """Tests for CLIChatSession.send_message()."""

    @pytest.mark.asyncio
    async def test_send_message_yields_chat_events(self) -> None:
        """send_message() should translate StreamEvents into ChatEvents."""
        session = _make_session()

        # Mock the internal CLI session to produce stream events
        mock_cli = AsyncMock()

        async def _fake_stream(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            yield MagicMock(type="text", text="Hello")

        mock_cli.send_message = _fake_stream
        session._cli_session = mock_cli

        events = []
        async for event in session.send_message("Hello"):
            events.append(event)

        assert len(events) > 0

    @pytest.mark.asyncio
    async def test_send_message_translates_stream_events(self) -> None:
        """send_message() should convert CLI stream events to ChatEvent format."""
        from gobby.llm.claude_models import ChatEvent

        session = _make_session()

        mock_cli = AsyncMock()

        async def _fake_stream(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            yield MagicMock(type="text", text="World")

        mock_cli.send_message = _fake_stream
        session._cli_session = mock_cli

        events = []
        async for event in session.send_message("test"):
            events.append(event)

        # All yielded events should be ChatEvent instances
        for event in events:
            assert isinstance(event, ChatEvent)


class TestInterrupt:
    """Tests for CLIChatSession.interrupt()."""

    @pytest.mark.asyncio
    async def test_interrupt_delegates_to_cli_session(self) -> None:
        """interrupt() should delegate to the underlying CLI session."""
        session = _make_session()

        mock_cli = AsyncMock()
        session._cli_session = mock_cli

        await session.interrupt()

        mock_cli.interrupt.assert_awaited_once()


class TestStop:
    """Tests for CLIChatSession.stop()."""

    @pytest.mark.asyncio
    async def test_stop_delegates_to_cli_session(self) -> None:
        """stop() should delegate to the underlying CLI session."""
        session = _make_session()

        mock_cli = AsyncMock()
        session._cli_session = mock_cli

        await session.stop()

        mock_cli.stop.assert_awaited_once()
