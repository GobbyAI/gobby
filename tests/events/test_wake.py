"""Tests for wake dispatcher."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import weakref
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from gobby.agents.tmux.text_injection import (
    TmuxTargetUnavailableError,
    TmuxTextInjectionTimeout,
)
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.events.wake import CONTINUE_WAKE_MESSAGE, CONTINUE_WAKE_SIGNAL, WakeDispatcher
from tests._timing import drain_asyncio_tasks

WAKE_SESSION_ID = "9264a39c-68db-5eed-917c-6f7babb8e6b1"
WAKE_RUN_ID = "ac314d27-4314-5fe3-a0ab-01645086e137"


def test_live_wake_signal_is_neutral() -> None:
    assert "Task completed" not in CONTINUE_WAKE_MESSAGE
    assert "Task completed" not in CONTINUE_WAKE_SIGNAL
    assert CONTINUE_WAKE_MESSAGE == "Message from Gobby daemon: New activity available."
    assert CONTINUE_WAKE_SIGNAL == f"{CONTINUE_WAKE_MESSAGE}\n"


@dataclass
class FakeSession:
    id: str
    agent_depth: int = 0
    terminal_context: object | None = None
    parent_session_id: str | None = None
    status: str = "paused"  # Completion subscribers normally wait between turns.
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
    @pytest.mark.parametrize(
        "status",
        ["interrupted", "awaiting_input", "awaiting_approval", "awaiting_handoff"],
    )
    async def test_protected_session_wake_touches_no_live_channel(
        self,
        status: str,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context={"tmux_session": "gobby-agent-abc", "tmux_pane": "%7"},
            status=status,
        )
        tmux_sender = AsyncMock()
        pane_sender = AsyncMock()
        sdk_resumer = AsyncMock()
        web_registry = MagicMock()
        web_registry.wake_session = AsyncMock()
        terminal_manager = MagicMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            tmux_pane_sender=pane_sender,
            sdk_resumer=sdk_resumer,
            web_chat_session_registry=web_registry,
            terminal_manager=terminal_manager,
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result == {
            "session_id": WAKE_SESSION_ID,
            "session_status": status,
            "delivered": False,
            "method": None,
            "skipped": f"session_{status}",
            "error_code": f"session_{status}",
            "decline_reason": f"session_{status}",
        }
        assert dispatcher._last_live_wake == {}
        terminal_manager.resolve_live_for_session.assert_not_called()
        tmux_sender.assert_not_awaited()
        pane_sender.assert_not_awaited()
        sdk_resumer.assert_not_awaited()
        web_registry.wake_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_completion_uses_next_call_context(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        tmux_sender: AsyncMock,
    ) -> None:
        """A routine completion must not interrupt an active tool batch."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context={"tmux_session": "gobby-agent-abc"},
            status="active",
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )

        result = await dispatcher.wake(
            WAKE_SESSION_ID,
            "Agent completed",
            {"status": "success"},
        )

        assert result == {
            "session_id": WAKE_SESSION_ID,
            "delivered": False,
            "method": "next_call_context",
            "skipped": "session_active",
            "decline_reason": "session_active",
            "ism_persisted": True,
        }
        tmux_sender.assert_not_awaited()
        assert ism_manager.create_message.call_args.kwargs["priority"] == "normal"

    @pytest.mark.asyncio
    async def test_urgent_active_completion_interrupts_immediately(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        tmux_sender: AsyncMock,
    ) -> None:
        """An explicitly urgent completion keeps the immediate wake path."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context={"tmux_session": "gobby-agent-abc"},
            status="active",
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )

        result = await dispatcher.wake(
            WAKE_SESSION_ID,
            "Agent requires attention",
            {"status": "failed", "priority": "urgent"},
        )

        assert result["delivered"] is True
        assert result["method"] == "tmux"
        assert result["ism_persisted"] is True
        tmux_sender.assert_awaited_once()
        assert ism_manager.create_message.call_args.kwargs["priority"] == "urgent"

    @pytest.mark.asyncio
    async def test_urgent_completion_stays_durable_when_the_composer_holds_a_draft(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        tmux_sender: AsyncMock,
    ) -> None:
        from gobby.agents.idle_detector import ComposerRead
        from gobby.events.live_wake import TerminalActivity, composer_occupied_result

        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context={"tmux_session": "gobby-agent-abc"},
            status="paused",
        )
        probe = AsyncMock(return_value=TerminalActivity(ComposerRead("draft", "hello draft")))
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            activity_probe=probe,
        )

        result = await dispatcher.wake(
            WAKE_SESSION_ID,
            "Agent requires attention",
            {"status": "failed", "priority": "urgent"},
        )

        assert result == composer_occupied_result(WAKE_SESSION_ID, method="tmux")
        assert ism_manager.create_message.call_args.kwargs["priority"] == "urgent"
        probe.assert_awaited_once()
        tmux_sender.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wake_routes_database_work_through_owned_executor(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        session_manager.get.return_value = FakeSession(id=WAKE_SESSION_ID, agent_depth=0)
        offloaded: list[str] = []

        async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
            offloaded.append(func.__name__)
            return func(*args, **kwargs)

        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            run_db=run_db,
        )

        result = await dispatcher.wake(WAKE_SESSION_ID, "done", {"status": "completed"})

        assert result["ism_persisted"] is True
        assert offloaded == ["read_session", "persist_notification", "read_session"]

    @pytest.mark.asyncio
    async def test_live_wake_internal_timeout_returns_structured_failure(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """The sender's own subprocess bound surfaces as a structured failure."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context={"tmux_pane": "%1"},
        )

        async def timing_out_sender(*_args: object, **_kwargs: object) -> None:
            raise TmuxTextInjectionTimeout(command=("tmux", "paste-buffer"), timeout=10.0)

        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=timing_out_sender,
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is False
        assert result["error_code"] == "tmux_pane_wake_failed"

    @pytest.mark.asyncio
    async def test_live_wake_slow_injection_is_not_cancelled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Injection slower than LIVE_WAKE_TIMEOUT_SECONDS completes and delivers."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context={"tmux_pane": "%1"},
        )
        completed = asyncio.Event()
        release = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, release.set)

        async def slow_sender(*_args: object, **_kwargs: object) -> None:
            await release.wait()
            completed.set()

        monkeypatch.setattr("gobby.events.wake.LIVE_WAKE_TIMEOUT_SECONDS", 0.01)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=slow_sender,
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert completed.is_set()
        assert result["delivered"] is True
        assert result["method"] == "tmux_pane"

    @pytest.mark.asyncio
    async def test_interactive_session_gets_ism(
        self, session_manager: MagicMock, ism_manager: MagicMock
    ) -> None:
        """agent_depth=0 → InterSessionMessage."""
        session_manager.get.return_value = FakeSession(id=WAKE_SESSION_ID, agent_depth=0)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )
        result = await dispatcher.wake(
            WAKE_SESSION_ID, "Pipeline completed", {"status": "completed"}
        )

        assert result["session_id"] == WAKE_SESSION_ID
        assert "delivered" in result
        assert result["ism_persisted"] is True
        ism_manager.create_message.assert_called_once()
        call_kwargs = ism_manager.create_message.call_args.kwargs
        assert call_kwargs["to_session"] == WAKE_SESSION_ID
        assert call_kwargs["message_type"] == "completion_notification"
        assert "Pipeline completed" in call_kwargs["content"]

    @pytest.mark.asyncio
    async def test_wake_result_contract_marks_ism_insert_failure(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        session_manager.get.return_value = FakeSession(id=WAKE_SESSION_ID, agent_depth=0)
        ism_manager.create_message.side_effect = RuntimeError("database unavailable")
        dispatcher = WakeDispatcher(session_manager=session_manager, ism_manager=ism_manager)

        result = await dispatcher.wake(
            WAKE_SESSION_ID,
            "Agent completed",
            {"status": "completed", "run_id": WAKE_RUN_ID},
        )

        assert result["ism_persisted"] is False
        assert result["error_code"] == "ism_persist_failed"

    @pytest.mark.asyncio
    async def test_wake_result_contract_marks_missing_session_terminal(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        session_manager.get.return_value = None
        dispatcher = WakeDispatcher(session_manager=session_manager, ism_manager=ism_manager)

        result = await dispatcher.wake(
            WAKE_SESSION_ID,
            "Agent completed",
            {"status": "completed", "run_id": WAKE_RUN_ID},
        )

        assert result["ism_persisted"] is False
        assert result["error_code"] == "session_not_found"

    @pytest.mark.asyncio
    async def test_terminal_agent_gets_tmux(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        tmux_sender: AsyncMock,
    ) -> None:
        """agent_depth>0 with terminal_context → durable ISM plus tmux wake."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context='{"tmux_session": "gobby-agent-abc", "tmux_pane": "%5"}',
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )
        await dispatcher.wake(WAKE_SESSION_ID, "Agent completed", {"status": "success"})

        ism_manager.create_message.assert_called_once()
        tmux_sender.assert_called_once()
        args = tmux_sender.call_args[0]
        assert args[0] == "gobby-agent-abc"  # tmux session name
        assert args[1] == CONTINUE_WAKE_MESSAGE
        assert tmux_sender.call_args.kwargs == {
            "submit": True,
            "clear_before_submit": True,
            "cli_source": ANY,
        }
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
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context={"tmux_session": "gobby-agent-abc", "tmux_pane": "%5"},
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )

        await dispatcher.wake(WAKE_SESSION_ID, "Agent completed", {"status": "success"})

        tmux_sender.assert_awaited_once_with(
            "gobby-agent-abc",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
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
            id=WAKE_SESSION_ID,
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

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is True
        assert result["method"] == "tmux_pane"
        tmux_sender.assert_not_awaited()
        tmux_pane_sender.assert_awaited_once_with(
            "%5",
            CONTINUE_WAKE_MESSAGE,
            "/tmp/tmux-501/gobby",
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
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
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context='{"tmux_session": "gobby-agent-abc", "tmux_pane": "%5"}',
        )
        failing_tmux = AsyncMock(side_effect=RuntimeError("tmux session dead"))

        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=failing_tmux,
        )
        await dispatcher.wake(WAKE_SESSION_ID, "Pipeline completed", {"status": "completed"})

        ism_manager.create_message.assert_called_once()
        assert ism_manager.create_message.call_count == 1
        assert ism_manager.create_message.call_args is not None
        failing_tmux.assert_awaited_once_with(
            "gobby-agent-abc",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
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
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context='{"tmux_session": "gobby-agent-abc"}',
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=None,
        )
        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed"})

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
        tmux_sender = AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("wake"))
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
                "run_id": WAKE_RUN_ID,
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
        assert f'"completion_id": "{WAKE_RUN_ID}"' in call_kwargs["metadata_json"]
        assert '"task_id": "#12754"' in call_kwargs["metadata_json"]
        tmux_sender.assert_awaited_once_with(
            "gobby-agent-parent",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )

    @pytest.mark.asyncio
    async def test_completion_notification_dedupes_by_completion_id(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """A replayed completion notification does not create duplicate ISM rows."""
        existing = MagicMock()
        existing.metadata_json = f'{{"completion_id": "{WAKE_RUN_ID}", "run_id": "{WAKE_RUN_ID}"}}'
        ism_manager.list_messages.return_value = [existing]
        session_manager.get.return_value = FakeSession(id=WAKE_SESSION_ID, agent_depth=0)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )

        registry = CompletionEventRegistry(wake_callback=dispatcher.wake)
        registry.register(WAKE_RUN_ID, subscribers=[WAKE_SESSION_ID])
        delivery = await registry.notify(
            WAKE_RUN_ID,
            {"status": "cancelled"},
            message="Agent interrupted",
        )

        assert delivery == {WAKE_SESSION_ID: True}
        assert registry.get_result(WAKE_RUN_ID) == {"status": "cancelled"}
        ism_manager.create_message.assert_not_called()
        assert ism_manager.create_message.call_count == 0
        assert not ism_manager.create_message.called

    @pytest.mark.asyncio
    async def test_registry_replay_dedupes_without_producer_run_or_execution_id(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """A fresh registry replay reuses its authoritative completion ID."""
        session_manager.get.return_value = FakeSession(id=WAKE_SESSION_ID, agent_depth=0)
        ism_manager.list_messages.return_value = []
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )

        first_registry = CompletionEventRegistry(wake_callback=dispatcher.wake)
        first_registry.register(WAKE_RUN_ID, subscribers=[WAKE_SESSION_ID])
        await first_registry.notify(WAKE_RUN_ID, {"status": "cancelled"})

        assert ism_manager.create_message.call_count == 1
        first_metadata = json.loads(ism_manager.create_message.call_args.kwargs["metadata_json"])
        assert first_metadata["completion_id"] == WAKE_RUN_ID
        assert "run_id" not in first_metadata
        assert "execution_id" not in first_metadata

        persisted = MagicMock(metadata_json=json.dumps(first_metadata))
        ism_manager.list_messages.return_value = [persisted]
        replay_registry = CompletionEventRegistry(wake_callback=dispatcher.wake)
        replay_registry.register(WAKE_RUN_ID, subscribers=[WAKE_SESSION_ID])
        await replay_registry.notify(WAKE_RUN_ID, {"status": "cancelled"})

        assert ism_manager.create_message.call_count == 1

    @pytest.mark.asyncio
    async def test_interactive_tmux_session_uses_repaired_active_pane(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Wake delivery uses the active pane persisted by liveness repair."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_window_id": "@7", "tmux_pane": "%19"}',
        )
        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed"})

        ism_manager.create_message.assert_called_once()
        assert ism_manager.create_message.call_count == 1
        assert ism_manager.create_message.call_args is not None
        tmux_pane_sender.assert_awaited_once_with(
            "%19",
            CONTINUE_WAKE_MESSAGE,
            None,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
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
            id=WAKE_SESSION_ID,
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

        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed"})

        tmux_pane_sender.assert_awaited_once_with(
            "%12",
            CONTINUE_WAKE_MESSAGE,
            "/tmp/tmux-501/gobby",
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
        assert tmux_pane_sender.await_count == 1
        assert tmux_pane_sender.await_args is not None

    @pytest.mark.asyncio
    async def test_expected_pane_wake_failure_returns_structured_result_without_warning(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Expected tmux pane failures return structured diagnostics without stack traces."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
        )
        tmux_pane_sender = AsyncMock(
            side_effect=TmuxTargetUnavailableError(
                "tmux target is unavailable: can't find pane: %12",
                command=("tmux", "paste-buffer"),
                stderr="can't find pane: %12",
                returncode=1,
            )
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        with caplog.at_level(logging.INFO, logger="gobby.events.wake"):
            result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is False
        assert result["method"] == "tmux_pane"
        assert result["error_code"] == "tmux_pane_wake_failed"
        assert "can't find pane" in result["error_message"]
        assert WAKE_SESSION_ID not in dispatcher._last_live_wake
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert not [record for record in caplog.records if record.exc_info]

    @pytest.mark.asyncio
    async def test_expired_session_returns_structured_wake_failure(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Expired sessions are durable-mailbox only and report why live wake skipped."""
        session_manager.get.return_value = FakeSession(id=WAKE_SESSION_ID, status="expired")
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is False
        assert result["error_code"] == "session_expired"

    @pytest.mark.asyncio
    async def test_transient_live_wake_failure_does_not_retain_lock(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Live wake locks for one-off missing sessions are not retained forever."""
        session_manager.get.return_value = None
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )

        result = await dispatcher.dispatch_live_wake("missing-session")
        gc.collect()

        assert result["error_code"] == "session_not_found"
        assert not dispatcher._live_wake_locks

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("entry", "args"),
        [
            ("wake", (WAKE_SESSION_ID, "done", {})),
            ("dispatch_live_wake", (WAKE_SESSION_ID,)),
            ("dispatch_live_wakes", ([WAKE_SESSION_ID],)),
        ],
    )
    async def test_wake_off_the_owner_loop_raises_before_touching_state(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        entry: str,
        args: tuple[object, ...],
    ) -> None:
        """Every wake entry rejects a foreign loop before reading, persisting, or locking."""
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )
        owner_loop = asyncio.new_event_loop()
        dispatcher.bind_owner_loop(owner_loop)
        try:
            with pytest.raises(RuntimeError, match="daemon event loop"):
                await getattr(dispatcher, entry)(*args)
        finally:
            owner_loop.close()

        assert not dispatcher._live_wake_locks
        session_manager.get.assert_not_called()
        ism_manager.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_deferred_lifecycle_refresh_failure_preserves_active_decline(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            terminal_context={"tmux_pane": "%12"},
            status="active",
        )
        lifecycle_refresh = AsyncMock(side_effect=RuntimeError("refresh failed"))
        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            lifecycle_refresh=lifecycle_refresh,
            tmux_pane_sender=tmux_pane_sender,
        )

        with caplog.at_level(logging.WARNING, logger="gobby.events.wake"):
            result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)
            await drain_asyncio_tasks()

        assert result["skipped"] == "session_active"
        lifecycle_refresh.assert_awaited_once_with(WAKE_SESSION_ID)
        tmux_pane_sender.assert_not_awaited()
        assert "Lifecycle refresh failed before retrying wake" in caplog.text

    @pytest.mark.asyncio
    async def test_idle_wake_does_not_wait_for_transcript_refresh(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            terminal_context={"tmux_pane": "%12"},
            status="paused",
        )
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()

        async def blocked_refresh(_session_id: str) -> None:
            refresh_started.set()
            await release_refresh.wait()

        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            lifecycle_refresh=blocked_refresh,
            tmux_pane_sender=tmux_pane_sender,
        )

        result = await asyncio.wait_for(dispatcher.dispatch_live_wake(WAKE_SESSION_ID), 0.5)
        assert result["delivered"] is True
        tmux_pane_sender.assert_awaited_once()
        assert not refresh_started.is_set()
        release_refresh.set()

    @pytest.mark.asyncio
    async def test_wake_declines_if_session_becomes_active_before_terminal_write(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        session_manager.get.side_effect = [
            FakeSession(
                id=WAKE_SESSION_ID,
                terminal_context={"tmux_pane": "%12"},
                status="paused",
            ),
            FakeSession(
                id=WAKE_SESSION_ID,
                terminal_context={"tmux_pane": "%12"},
                status="active",
            ),
        ]
        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["skipped"] == "session_active"
        tmux_pane_sender.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_active_wake_retries_after_deferred_refresh(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        session = FakeSession(
            id=WAKE_SESSION_ID,
            terminal_context={"tmux_pane": "%12"},
            status="active",
        )
        session_manager.get.return_value = session
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()
        wake_delivered = asyncio.Event()

        async def blocked_refresh(_session_id: str) -> None:
            refresh_started.set()
            await release_refresh.wait()
            session.status = "paused"

        async def send_pane(*_args: object, **_kwargs: object) -> None:
            wake_delivered.set()

        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            lifecycle_refresh=blocked_refresh,
            tmux_pane_sender=send_pane,
        )

        result = await asyncio.wait_for(dispatcher.dispatch_live_wake(WAKE_SESSION_ID), 0.5)
        assert result["skipped"] == "session_active"
        await asyncio.wait_for(refresh_started.wait(), 0.5)
        release_refresh.set()
        await asyncio.wait_for(wake_delivered.wait(), 0.5)

    @pytest.mark.asyncio
    async def test_interactive_session_without_tmux_pane_reports_no_tmux_pane(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Terminal context without a pane gets a precise live wake diagnostic."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context={"parent_pid": 12345},
        )
        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

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
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context={"tmux_pane": "%12"},
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

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
        result = await dispatcher.wake("nonexistent", "Done", {"status": "completed"})
        assert result["error_code"] == "session_not_found"
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
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_session": "some-session"}',
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
        )
        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed"})

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
            id=WAKE_SESSION_ID,
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

        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r1"})
        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r2"})
        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r3"})

        tmux_pane_sender.assert_awaited_once_with(
            "%12",
            CONTINUE_WAKE_MESSAGE,
            None,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
        assert ism_manager.create_message.call_count == 3

    @pytest.mark.asyncio
    async def test_concurrent_pane_wakes_coalesce_before_sending_text(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Concurrent completions must not interleave duplicate wake prompts in the pane."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )

        async def slow_pane_send(
            _pane_id: str,
            _message: str,
            _socket_path: str | None,
            *,
            submit: bool = False,
            clear_before_submit: bool = False,
            cli_source: str | None = None,
        ) -> None:
            assert submit is True
            assert clear_before_submit is True
            send_started.set()
            await release_send.wait()

        send_started = asyncio.Event()
        release_send = asyncio.Event()
        tmux_pane_sender = AsyncMock(side_effect=slow_pane_send)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        async def run_wakes() -> list[None]:
            return await asyncio.gather(
                dispatcher.wake(
                    WAKE_SESSION_ID,
                    "Done",
                    {"status": "completed", "run_id": "r1"},
                ),
                dispatcher.wake(
                    WAKE_SESSION_ID,
                    "Done",
                    {"status": "completed", "run_id": "r2"},
                ),
                dispatcher.wake(
                    WAKE_SESSION_ID,
                    "Done",
                    {"status": "completed", "run_id": "r3"},
                ),
            )

        wakes = asyncio.create_task(run_wakes())
        await asyncio.wait_for(send_started.wait(), timeout=1)
        await drain_asyncio_tasks(cycles=2)
        release_send.set()
        await wakes

        tmux_pane_sender.assert_awaited_once_with(
            "%12",
            CONTINUE_WAKE_MESSAGE,
            None,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
        assert ism_manager.create_message.call_count == 3

    @pytest.mark.asyncio
    async def test_concurrent_terminal_agent_wakes_coalesce_to_one_live_signal(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Terminal agents need one wake signal; durable ISMs carry distinct completions."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context='{"tmux_session": "gobby-agent-abc", "tmux_pane": "%5"}',
            turn_count=8,
        )

        async def slow_tmux_send(
            _tmux_session_name: str,
            _message: str,
            *,
            submit: bool = False,
            clear_before_submit: bool = False,
            cli_source: str | None = None,
        ) -> None:
            assert submit is True
            assert clear_before_submit is True
            send_started.set()
            await release_send.wait()

        send_started = asyncio.Event()
        release_send = asyncio.Event()
        tmux_sender = AsyncMock(side_effect=slow_tmux_send)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )

        async def run_wakes() -> list[None]:
            return await asyncio.gather(
                dispatcher.wake(
                    WAKE_SESSION_ID,
                    "Done",
                    {"status": "completed", "run_id": "r1"},
                ),
                dispatcher.wake(
                    WAKE_SESSION_ID,
                    "Done",
                    {"status": "completed", "run_id": "r2"},
                ),
                dispatcher.wake(
                    WAKE_SESSION_ID,
                    "Done",
                    {"status": "completed", "run_id": "r3"},
                ),
            )

        wakes = asyncio.create_task(run_wakes())
        await asyncio.wait_for(send_started.wait(), timeout=1)
        await drain_asyncio_tasks(cycles=2)
        release_send.set()
        await wakes

        tmux_sender.assert_awaited_once_with(
            "gobby-agent-abc",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
        assert ism_manager.create_message.call_count == 3

    @pytest.mark.asyncio
    async def test_pane_wake_resumes_after_turn_advances(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Once the user takes a new turn, the next completion fires a pane wake again."""
        first_session = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )
        second_session = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=6,
        )
        session_manager.get.side_effect = [
            first_session,
            first_session,
            first_session,
            second_session,
            second_session,
            second_session,
        ]

        tmux_pane_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r1"})
        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r2"})

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
            id=WAKE_SESSION_ID,
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

        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r1"})
        clock[0] += 5.0
        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r2"})
        assert tmux_pane_sender.await_count == 1
        clock[0] += 31.0
        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r3"})

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
        stale_lock = asyncio.Lock()
        fresh_lock = asyncio.Lock()
        dispatcher._last_live_wake = {
            "stale": (1, 900.0),
            "locked": (1, 900.0),
            "fresh": (1, 990.0),
        }
        dispatcher._live_wake_locks = weakref.WeakValueDictionary(
            {"stale": stale_lock, "locked": locked, "fresh": fresh_lock}
        )
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
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Expected pane failures stay retryable and do not emit warning stack traces."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )
        tmux_pane_sender = AsyncMock(
            side_effect=[
                TmuxTargetUnavailableError(
                    "tmux target is unavailable: can't find pane: %12",
                    command=("tmux", "paste-buffer"),
                    stderr="can't find pane: %12",
                    returncode=1,
                ),
                None,
            ]
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_pane_sender=tmux_pane_sender,
        )

        with caplog.at_level(logging.INFO, logger="gobby.events.wake"):
            await dispatcher.wake(
                WAKE_SESSION_ID,
                "Done",
                {"status": "completed", "run_id": "r1"},
            )

        assert WAKE_SESSION_ID not in dispatcher._last_live_wake
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert not [record for record in caplog.records if record.exc_info]

        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r2"})

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
            wake_started.set()
            await release_wake.wait()
            return {
                "session_id": "web-1",
                "delivered": True,
                "method": "web_chat",
                "queued": False,
            }

        wake_started = asyncio.Event()
        release_wake = asyncio.Event()
        registry = MagicMock()
        registry.wake_session = AsyncMock(side_effect=slow_web_wake)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            web_chat_session_registry=registry,
        )

        async def run_wakes() -> list[dict[str, object]]:
            return await asyncio.gather(
                dispatcher.dispatch_live_wake("web-1"),
                dispatcher.dispatch_live_wake("web-1"),
                dispatcher.dispatch_live_wake("web-1"),
            )

        wakes = asyncio.create_task(run_wakes())
        await asyncio.wait_for(wake_started.wait(), timeout=1)
        await drain_asyncio_tasks(cycles=2)
        release_wake.set()
        results = await wakes

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


class TestComposerGate:
    """A positive draft read withholds the live wake; anything else drains as before."""

    @staticmethod
    def _dispatcher(probe: object, pane_sender: AsyncMock) -> WakeDispatcher:
        from gobby.events.live_wake import ActivityProbe

        session_manager = MagicMock()
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID, terminal_context={"tmux_pane": "%7"}
        )
        terminal_manager = MagicMock()
        terminal_manager.resolve_live_for_session.return_value = None
        return WakeDispatcher(
            session_manager=session_manager,
            ism_manager=MagicMock(),
            tmux_sender=AsyncMock(),
            tmux_pane_sender=pane_sender,
            terminal_manager=terminal_manager,
            activity_probe=cast("ActivityProbe | None", probe),
        )

    @pytest.mark.asyncio
    async def test_draft_defers_the_wake_to_the_next_turn(self) -> None:
        from gobby.agents.idle_detector import ComposerRead
        from gobby.events.live_wake import TerminalActivity

        pane_sender = AsyncMock()
        probe = AsyncMock(return_value=TerminalActivity(ComposerRead("draft", "hello draft")))
        dispatcher = self._dispatcher(probe, pane_sender)

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result == {
            "session_id": WAKE_SESSION_ID,
            "delivered": False,
            "method": "tmux_pane",
            "skipped": "composer_occupied",
            "decline_reason": "composer_occupied",
            "ism_persisted": True,
        }
        pane_sender.assert_not_awaited()
        assert dispatcher._last_live_wake == {}

    @pytest.mark.asyncio
    async def test_urgent_wake_defers_when_the_composer_holds_a_draft(self) -> None:
        from gobby.agents.idle_detector import ComposerRead
        from gobby.events.live_wake import TerminalActivity, composer_occupied_result

        pane_sender = AsyncMock()
        probe = AsyncMock(return_value=TerminalActivity(ComposerRead("draft", "hello draft")))
        dispatcher = self._dispatcher(probe, pane_sender)

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID, priority="urgent")

        assert result == composer_occupied_result(WAKE_SESSION_ID, method="tmux_pane")
        probe.assert_awaited_once()
        pane_sender.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_urgent_wake_delivers_when_the_composer_is_empty(self) -> None:
        from gobby.agents.idle_detector import ComposerRead
        from gobby.events.live_wake import TerminalActivity

        pane_sender = AsyncMock()
        probe = AsyncMock(return_value=TerminalActivity(ComposerRead("empty")))
        dispatcher = self._dispatcher(probe, pane_sender)

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID, priority="urgent")

        assert result["delivered"] is True
        probe.assert_awaited_once()
        pane_sender.assert_awaited_once_with(
            "%7", CONTINUE_WAKE_MESSAGE, None, submit=True, clear_before_submit=True, cli_source=ANY
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["empty", "unknown"])
    async def test_non_draft_reads_drain_blind(self, state: str) -> None:
        from gobby.agents.idle_detector import ComposerRead, ComposerState
        from gobby.events.live_wake import TerminalActivity

        pane_sender = AsyncMock()
        probe = AsyncMock(return_value=TerminalActivity(ComposerRead(cast(ComposerState, state))))
        dispatcher = self._dispatcher(probe, pane_sender)

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is True
        pane_sender.assert_awaited_once_with(
            "%7", CONTINUE_WAKE_MESSAGE, None, submit=True, clear_before_submit=True, cli_source=ANY
        )

    @pytest.mark.asyncio
    async def test_probe_error_drains_blind(self) -> None:
        pane_sender = AsyncMock()
        dispatcher = self._dispatcher(AsyncMock(side_effect=RuntimeError("no pane")), pane_sender)

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is True
        pane_sender.assert_awaited_once()
