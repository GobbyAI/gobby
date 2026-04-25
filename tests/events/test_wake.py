"""Tests for wake dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.events.wake import CONTINUE_WAKE_SIGNAL, WakeDispatcher


@dataclass
class FakeSession:
    id: str
    agent_depth: int = 0
    terminal_context: object | None = None
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
        """agent_depth>0 with terminal_context → durable ISM plus tmux wake."""
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

        ism_manager.create_message.assert_called_once()
        tmux_sender.assert_called_once()
        args = tmux_sender.call_args[0]
        assert args[0] == "gobby-agent-abc"  # tmux session name
        assert args[1] == CONTINUE_WAKE_SIGNAL

    @pytest.mark.asyncio
    async def test_terminal_agent_accepts_mapping_terminal_context(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        tmux_sender: AsyncMock,
    ) -> None:
        """terminal_context may already be a parsed mapping."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=1,
            terminal_context={"tmux_session": "gobby-agent-abc", "tmux_pane": "%5"},
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )

        await dispatcher.wake("sess-1", "Agent completed", {"status": "success"})

        tmux_sender.assert_awaited_once_with("gobby-agent-abc", CONTINUE_WAKE_SIGNAL)

    @pytest.mark.asyncio
    async def test_terminal_agent_fallback_to_ism_when_tmux_fails(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Durable ISM remains when tmux wake fails."""
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

        ism_manager.create_message.assert_called_once()
        failing_tmux.assert_awaited_once_with("gobby-agent-abc", CONTINUE_WAKE_SIGNAL)

    @pytest.mark.asyncio
    async def test_terminal_agent_no_tmux_sender_uses_ism(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Terminal agent without tmux_sender still gets durable ISM."""
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
    async def test_parent_signoff_persists_durable_message_before_wake(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Parent signoff delivery stores the payload before the wake signal."""
        events: list[str] = []
        session_manager.get.return_value = FakeSession(
            id="parent-1",
            agent_depth=1,
            terminal_context={"tmux_session": "gobby-agent-parent"},
        )
        ism_manager.list_messages.return_value = []
        ism_manager.create_message.side_effect = lambda **_kwargs: events.append("ism")
        tmux_sender = AsyncMock(side_effect=lambda *_args: events.append("wake"))
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )

        await dispatcher.wake(
            "parent-1",
            "Agent signed off",
            {
                "message_type": "completion_notification",
                "from_session_id": "child-1",
                "run_id": "run-1",
                "task_id": "#12754",
                "signoff_message": "Review approved",
            },
        )

        assert events == ["ism", "wake"]
        call_kwargs = ism_manager.create_message.call_args.kwargs
        assert call_kwargs["from_session"] == "child-1"
        assert call_kwargs["to_session"] == "parent-1"
        assert call_kwargs["content"] == "Review approved"
        assert call_kwargs["message_type"] == "completion_notification"
        assert '"completion_id": "run-1"' in call_kwargs["metadata_json"]
        assert '"task_id": "#12754"' in call_kwargs["metadata_json"]
        tmux_sender.assert_awaited_once_with("gobby-agent-parent", CONTINUE_WAKE_SIGNAL)

    @pytest.mark.asyncio
    async def test_completion_notification_dedupes_by_completion_id(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """A replayed completion notification does not create duplicate ISM rows."""
        existing = MagicMock()
        existing.metadata_json = '{"completion_id": "run-1", "run_id": "run-1"}'
        ism_manager.list_messages.return_value = [existing]
        session_manager.get.return_value = FakeSession(id="sess-1", agent_depth=0)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )

        await dispatcher.wake(
            "sess-1",
            "Agent interrupted",
            {"status": "cancelled", "run_id": "run-1"},
        )

        ism_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_interactive_tmux_session_gets_pane_wake_signal(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Depth 0 tmux-backed sessions get durable ISM plus pane wake."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
        )
        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        await dispatcher.wake("sess-1", "Done", {"status": "completed"})

        ism_manager.create_message.assert_called_once()
        tmux_pane_sender.assert_awaited_once_with("%12", CONTINUE_WAKE_SIGNAL, None)

    @pytest.mark.asyncio
    async def test_interactive_tmux_session_uses_stored_socket_path(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Depth 0 tmux-backed sessions use the recorded tmux socket."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context={
                "tmux_pane": "%12",
                "tmux_socket_path": "/tmp/tmux-501/gobby",
            },
        )
        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        await dispatcher.wake("sess-1", "Done", {"status": "completed"})

        tmux_pane_sender.assert_awaited_once_with(
            "%12",
            CONTINUE_WAKE_SIGNAL,
            "/tmp/tmux-501/gobby",
        )

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
