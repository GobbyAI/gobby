"""Tests for wake dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.events.wake import WakeDispatcher


@dataclass
class FakeSession:
    id: str
    agent_depth: int = 0
    terminal_context: str | None = None
    parent_session_id: str | None = None
    status: str = "active"


@pytest.fixture
def session_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.get.return_value = None
    return mgr


@pytest.fixture
def ism_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.create_message = MagicMock()
    return mgr


@pytest.fixture
def tmux_sender() -> AsyncMock:
    return AsyncMock()


class TestWakeDispatch:
    """Route wake messages based on session type."""

    @pytest.mark.asyncio
    async def test_interactive_session_gets_ism(
        self, session_manager: MagicMock, ism_manager: MagicMock
    ) -> None:
        """agent_depth=0 → InterSessionMessage."""
        session_manager.get.return_value = FakeSession(id="sess-1", agent_depth=0)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )
        await dispatcher.wake("sess-1", "Pipeline completed", {"status": "completed"})

        ism_manager.create_message.assert_called_once()
        call_kwargs = ism_manager.create_message.call_args.kwargs
        assert call_kwargs["to_session"] == "sess-1"
        assert call_kwargs["message_type"] == "completion_notification"
        assert "Pipeline completed" in call_kwargs["content"]

    @pytest.mark.asyncio
    async def test_terminal_agent_gets_tmux(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        tmux_sender: AsyncMock,
    ) -> None:
        """agent_depth>0 with terminal_context → tmux send-keys."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=1,
            terminal_context='{"tmux_session": "gobby-agent-abc", "tmux_pane": "%5"}',
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )
        await dispatcher.wake("sess-1", "Agent completed", {"status": "success"})

        tmux_sender.assert_called_once()
        args = tmux_sender.call_args[0]
        assert args[0] == "gobby-agent-abc"  # tmux session name
        assert "Agent completed" in args[1]  # message sent

    @pytest.mark.asyncio
    async def test_terminal_agent_fallback_to_ism_when_tmux_fails(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """If tmux send fails, fall back to ISM."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=1,
            terminal_context='{"tmux_session": "gobby-agent-abc", "tmux_pane": "%5"}',
        )
        failing_tmux = AsyncMock(side_effect=RuntimeError("tmux session dead"))

        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=failing_tmux,
        )
        await dispatcher.wake("sess-1", "Pipeline completed", {"status": "completed"})

        # Should fall back to ISM
        ism_manager.create_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminal_agent_no_tmux_sender_uses_ism(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Terminal agent but no tmux_sender configured → ISM fallback."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=1,
            terminal_context='{"tmux_session": "gobby-agent-abc"}',
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=None,
        )
        await dispatcher.wake("sess-1", "Done", {"status": "completed"})

        ism_manager.create_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_session_logged_not_raised(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """If session not found, log warning but don't raise."""
        session_manager.get.return_value = None
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )
        # Should not raise
        await dispatcher.wake("nonexistent", "Done", {"status": "completed"})
        ism_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_depth_zero_no_terminal_context_gets_ism(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Depth 0 session always gets ISM regardless of terminal_context."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context='{"tmux_session": "some-session"}',
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )
        await dispatcher.wake("sess-1", "Done", {"status": "completed"})

        ism_manager.create_message.assert_called_once()
