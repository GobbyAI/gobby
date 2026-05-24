"""Tests for wake dispatcher."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.events.wake import CONTINUE_WAKE_SIGNAL, WakeDispatcher


def test_live_wake_signal_is_neutral() -> None:
    assert "Task completed" not in CONTINUE_WAKE_SIGNAL
    assert CONTINUE_WAKE_SIGNAL == "Message from Gobby daemon: New activity available.\n"


@dataclass
class FakeSession:
    id: str
    agent_depth: int = 0
    terminal_context: object | None = None
    parent_session_id: str | None = None
    status: str = "active"
    turn_count: int = 0
    session_type: str = "terminal"


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
        assert "Task completed" not in args[1]
        call_kwargs = ism_manager.create_message.call_args.kwargs
        assert call_kwargs["content"] == "Agent completed"

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
        assert tmux_sender.await_count == 1
        assert tmux_sender.await_args is not None

    @pytest.mark.asyncio
    async def test_terminal_agent_uses_tmux_pane_when_session_name_missing(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Terminal child agents can be nudged from pane-only terminal context."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=1,
            terminal_context={
                "tmux_pane": "%5",
                "tmux_socket_path": "/tmp/tmux-501/gobby",
            },
        )
        tmux_sender = AsyncMock()
        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            tmux_pane_sender=tmux_pane_sender,
        )

        result = await dispatcher.dispatch_live_wake("sess-1")

        assert result["delivered"] is True
        assert result["method"] == "tmux_pane"
        tmux_sender.assert_not_awaited()
        tmux_pane_sender.assert_awaited_once_with(
            "%5",
            CONTINUE_WAKE_SIGNAL,
            "/tmp/tmux-501/gobby",
        )
        assert "Task completed" not in tmux_pane_sender.await_args.args[1]

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
        assert ism_manager.create_message.call_count == 1
        assert ism_manager.create_message.call_args is not None
        failing_tmux.assert_awaited_once_with("gobby-agent-abc", CONTINUE_WAKE_SIGNAL)
        assert failing_tmux.await_count == 1
        assert failing_tmux.await_args is not None

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
        assert ism_manager.create_message.call_count == 1
        assert ism_manager.create_message.call_args is not None

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
        assert ism_manager.create_message.call_count == 0
        assert not ism_manager.create_message.called

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
        assert ism_manager.create_message.call_count == 1
        assert ism_manager.create_message.call_args is not None
        tmux_pane_sender.assert_awaited_once_with("%12", CONTINUE_WAKE_SIGNAL, None)
        assert "Task completed" not in tmux_pane_sender.await_args.args[1]
        call_kwargs = ism_manager.create_message.call_args.kwargs
        assert call_kwargs["content"] == "Done"
        assert tmux_pane_sender.await_count == 1
        assert tmux_pane_sender.await_args is not None

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
        assert tmux_pane_sender.await_count == 1
        assert tmux_pane_sender.await_args is not None

    @pytest.mark.asyncio
    async def test_expired_session_returns_structured_wake_failure(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Expired sessions are durable-mailbox only and report why live wake skipped."""
        session_manager.get.return_value = FakeSession(id="sess-1", status="expired")
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )

        result = await dispatcher.dispatch_live_wake("sess-1")

        assert result["delivered"] is False
        assert result["error_code"] == "session_expired"

    @pytest.mark.asyncio
    async def test_interactive_session_without_tmux_pane_reports_no_tmux_pane(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Terminal context without a pane gets a precise live wake diagnostic."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context={"parent_pid": 12345},
        )
        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        result = await dispatcher.dispatch_live_wake("sess-1")

        assert result["delivered"] is False
        assert result["method"] == "tmux_pane"
        assert result["error_code"] == "no_tmux_pane"
        tmux_pane_sender.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_interactive_session_without_sender_reports_no_live_channel(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """A recorded pane still needs a configured live sender."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context={"tmux_pane": "%12"},
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )

        result = await dispatcher.dispatch_live_wake("sess-1")

        assert result["delivered"] is False
        assert result["method"] == "tmux_pane"
        assert result["error_code"] == "no_live_wake_channel"

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
        assert ism_manager.create_message.call_count == 0
        assert not ism_manager.create_message.called

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
        assert ism_manager.create_message.call_count == 1
        assert ism_manager.create_message.call_args is not None

    @pytest.mark.asyncio
    async def test_pane_wake_coalesces_during_idle_window(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Bursty completions during one idle turn → one pane nudge, every ISM stored."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )
        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        await dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r1"})
        await dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r2"})
        await dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r3"})

        tmux_pane_sender.assert_awaited_once_with("%12", CONTINUE_WAKE_SIGNAL, None)
        assert ism_manager.create_message.call_count == 3

    @pytest.mark.asyncio
    async def test_concurrent_pane_wakes_coalesce_before_sending_text(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Concurrent completions must not interleave duplicate wake prompts in the pane."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )

        async def slow_pane_send(
            _pane_id: str,
            _message: str,
            _socket_path: str | None,
        ) -> None:
            await asyncio.sleep(0.01)

        tmux_pane_sender = AsyncMock(side_effect=slow_pane_send)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        await asyncio.gather(
            dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r1"}),
            dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r2"}),
            dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r3"}),
        )

        tmux_pane_sender.assert_awaited_once_with("%12", CONTINUE_WAKE_SIGNAL, None)
        assert ism_manager.create_message.call_count == 3

    @pytest.mark.asyncio
    async def test_concurrent_terminal_agent_wakes_coalesce_to_one_live_signal(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Terminal agents need one wake signal; durable ISMs carry distinct completions."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=1,
            terminal_context='{"tmux_session": "gobby-agent-abc", "tmux_pane": "%5"}',
            turn_count=8,
        )

        async def slow_tmux_send(_tmux_session_name: str, _message: str) -> None:
            await asyncio.sleep(0.01)

        tmux_sender = AsyncMock(side_effect=slow_tmux_send)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )

        await asyncio.gather(
            dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r1"}),
            dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r2"}),
            dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r3"}),
        )

        tmux_sender.assert_awaited_once_with("gobby-agent-abc", CONTINUE_WAKE_SIGNAL)
        assert ism_manager.create_message.call_count == 3

    @pytest.mark.asyncio
    async def test_pane_wake_resumes_after_turn_advances(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Once the user takes a new turn, the next completion fires a pane wake again."""
        first_session = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )
        second_session = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=6,
        )
        session_manager.get.side_effect = [first_session, second_session]

        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        await dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r1"})
        await dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r2"})

        assert tmux_pane_sender.await_count == 2

    @pytest.mark.asyncio
    async def test_pane_wake_resumes_after_debounce_ceiling(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stuck idle longer than the 30s ceiling → next completion fires again."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )
        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        clock = [1000.0]

        def fake_monotonic() -> float:
            return clock[0]

        monkeypatch.setattr("gobby.events.wake.time.monotonic", fake_monotonic)

        await dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r1"})
        clock[0] += 5.0
        await dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r2"})
        assert tmux_pane_sender.await_count == 1
        clock[0] += 31.0
        await dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r3"})

        assert tmux_pane_sender.await_count == 2

    @pytest.mark.asyncio
    async def test_live_wake_prunes_stale_timestamps_and_unused_locks(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stale wake state cleanup removes idle locks but leaves active dispatch locks."""
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )
        locked = asyncio.Lock()
        await locked.acquire()
        dispatcher._last_live_wake = {
            "stale": (1, 900.0),
            "locked": (1, 900.0),
            "fresh": (1, 990.0),
        }
        dispatcher._live_wake_locks = {
            "stale": asyncio.Lock(),
            "locked": locked,
            "fresh": asyncio.Lock(),
        }
        monkeypatch.setattr("gobby.events.wake.time.monotonic", lambda: 1000.0)

        try:
            assert dispatcher._should_send_live_wake("new", FakeSession(id="new")) is True
        finally:
            locked.release()

        assert "stale" not in dispatcher._last_live_wake
        assert "stale" not in dispatcher._live_wake_locks
        assert "locked" in dispatcher._last_live_wake
        assert "locked" in dispatcher._live_wake_locks
        assert "fresh" in dispatcher._last_live_wake
        assert "fresh" in dispatcher._live_wake_locks

    @pytest.mark.asyncio
    async def test_pane_wake_failure_does_not_record_timestamp(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """If send-keys raises, the next completion should still try to wake the pane."""
        session_manager.get.return_value = FakeSession(
            id="sess-1",
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )
        tmux_pane_sender = AsyncMock(side_effect=[RuntimeError("boom"), None])
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        await dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r1"})
        await dispatcher.wake("sess-1", "Done", {"status": "completed", "run_id": "r2"})

        assert tmux_pane_sender.await_count == 2

    @pytest.mark.asyncio
    async def test_web_chat_session_routes_live_wake_through_registry(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """web_chat sessions use the live web-chat registry wake path."""
        session_manager.get.return_value = FakeSession(
            id="web-1",
            session_type="web_chat",
        )
        registry = MagicMock()
        registry.wake_session = AsyncMock(
            return_value={
                "session_id": "web-1",
                "delivered": True,
                "method": "web_chat",
                "queued": False,
            }
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            web_chat_session_registry=registry,
        )

        result = await dispatcher.dispatch_live_wake("web-1")

        assert result == {
            "session_id": "web-1",
            "delivered": True,
            "method": "web_chat",
            "queued": False,
        }
        registry.wake_session.assert_awaited_once_with("web-1")

    @pytest.mark.asyncio
    async def test_concurrent_web_chat_wakes_coalesce_to_one_hidden_turn(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """A web-chat session should not receive duplicate hidden wake prompts at once."""
        session_manager.get.return_value = FakeSession(
            id="web-1",
            session_type="web_chat",
            turn_count=12,
        )

        async def slow_web_wake(_session_id: str) -> dict[str, object]:
            await asyncio.sleep(0.01)
            return {
                "session_id": "web-1",
                "delivered": True,
                "method": "web_chat",
                "queued": False,
            }

        registry = MagicMock()
        registry.wake_session = AsyncMock(side_effect=slow_web_wake)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            web_chat_session_registry=registry,
        )

        results = await asyncio.gather(
            dispatcher.dispatch_live_wake("web-1"),
            dispatcher.dispatch_live_wake("web-1"),
            dispatcher.dispatch_live_wake("web-1"),
        )

        registry.wake_session.assert_awaited_once_with("web-1")
        assert [result.get("skipped") for result in results].count("debounced") == 2

    @pytest.mark.asyncio
    async def test_web_chat_session_without_live_registry_returns_explicit_failure(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """web_chat wake failures identify the missing live session case."""
        session_manager.get.return_value = FakeSession(
            id="web-1",
            session_type="web_chat",
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )

        result = await dispatcher.dispatch_live_wake("web-1")

        assert result["delivered"] is False
        assert result["method"] == "web_chat"
        assert result["error_code"] == "no_live_web_chat_session"
