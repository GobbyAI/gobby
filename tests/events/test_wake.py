"""Tests for wake dispatcher."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from gobby.agents.idle_detector import ComposerRead, IdleDetector
from gobby.agents.tmux.text_injection import (
    TmuxTextInjectionTimeout,
)
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake import CONTINUE_WAKE_MESSAGE, CONTINUE_WAKE_SIGNAL, WakeDispatcher
from gobby.events.wake_active_recovery import reconcile_idle_prompt_session
from gobby.storage.session_models import Session
from gobby.terminals.runtime import AutomaticWriteDeclined, AutomaticWriteQuarantined
from tests._timing import drain_asyncio_tasks
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.events.wake_test_support import PendingWakeLedger

WAKE_SESSION_ID = "9264a39c-68db-5eed-917c-6f7babb8e6b1"
WAKE_RUN_ID = "ac314d27-4314-5fe3-a0ab-01645086e137"


def _claude_idle_prompt() -> TerminalActivity:
    """A Claude pane sitting at an empty prompt, the same shape as the live detector."""
    snapshot = "\n".join(
        (
            "⏺ done",
            "──────────── epic-22508-feedback-triage ─",
            "❯",
            "────────────────────",
            "   Fable 5.1  12%   ⎇ main",
        )
    )
    detector = IdleDetector(BundledDetectionRegistry(), "claude")
    return TerminalActivity(detector.composer_read(snapshot))


def _codex_idle_prompt() -> TerminalActivity:
    snapshot = (
        "────────────\n\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n────────────\n"
    )
    detector = IdleDetector(BundledDetectionRegistry(), "codex")
    assert detector.reads_composer()
    return TerminalActivity(detector.composer_read(snapshot))


@pytest.mark.parametrize("race", ["composer_changed", "row_changed", "source_changed"])
async def test_codex_idle_recovery_rechecks_composer_and_exact_row(race: str) -> None:
    observed = FakeSession(
        id=WAKE_SESSION_ID,
        status="active",
        source="codex",
        updated_at=datetime(2026, 9, 27, 1, 29, tzinfo=UTC),
    )
    current = observed
    if race == "row_changed":
        assert observed.updated_at is not None
        current = replace(observed, updated_at=observed.updated_at + timedelta(seconds=1))
    elif race == "source_changed":
        current = replace(observed, source="claude")
    manager = MagicMock()
    manager.get.return_value = current
    idle = _codex_idle_prompt()
    reads = [idle, TerminalActivity(ComposerRead("draft")) if race == "composer_changed" else idle]

    async def probe(_session: object, _terminal: object | None) -> TerminalActivity:
        return reads.pop(0)

    async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    result = await reconcile_idle_prompt_session(
        session_manager=manager,
        observed=cast(Session, observed),
        terminal=object(),
        activity_probe=probe,
        run_db=run_db,
    )

    assert result == ("composer_draft" if race == "composer_changed" else "row_changed")
    assert reads == []
    manager._pause_idle_prompt_active.assert_not_called()


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
    source: str | None = None
    updated_at: datetime | None = None


def _managed_terminal() -> MagicMock:
    manager = MagicMock()
    manager.resolve_live_for_session.return_value = MagicMock(id="terminal-1")
    return manager


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
        sdk_resumer = AsyncMock()
        activity_probe = AsyncMock()
        web_registry = MagicMock()
        web_registry.wake_session = AsyncMock()
        terminal_manager = MagicMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            sdk_resumer=sdk_resumer,
            web_chat_session_registry=web_registry,
            terminal_manager=terminal_manager,
            activity_probe=activity_probe,
        )
        ledger = PendingWakeLedger(dispatcher)

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
        assert ledger.recorded == []
        terminal_manager.resolve_live_for_session.assert_not_called()
        tmux_sender.assert_not_awaited()
        sdk_resumer.assert_not_awaited()
        activity_probe.assert_not_awaited()
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
            terminal_manager=_managed_terminal(),
        )

        result = await dispatcher.wake(
            WAKE_SESSION_ID,
            "Agent requires attention",
            {"status": "failed", "priority": "urgent"},
        )

        assert result["delivered"] is True
        assert result["method"] == "terminal"
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
            terminal_manager=_managed_terminal(),
            activity_probe=probe,
        )

        result = await dispatcher.wake(
            WAKE_SESSION_ID,
            "Agent requires attention",
            {"status": "failed", "priority": "urgent"},
        )

        assert result == composer_occupied_result(WAKE_SESSION_ID, method="terminal")
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
            tmux_sender=timing_out_sender,
            terminal_manager=_managed_terminal(),
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is False
        assert result["error_code"] == "terminal_wake_failed"

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
            tmux_sender=slow_sender,
            terminal_manager=_managed_terminal(),
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert completed.is_set()
        assert result["delivered"] is True
        assert result["method"] == "terminal"

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
        """A managed agent terminal receives a wake after durable ISM storage."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context='{"tmux_session": "gobby-agent-abc", "tmux_pane": "%5"}',
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
        )
        await dispatcher.wake(WAKE_SESSION_ID, "Agent completed", {"status": "success"})

        ism_manager.create_message.assert_called_once()
        tmux_sender.assert_called_once()
        args = tmux_sender.call_args[0]
        assert args[0] == "terminal-1"
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
            terminal_manager=_managed_terminal(),
        )

        await dispatcher.wake(WAKE_SESSION_ID, "Agent completed", {"status": "success"})

        tmux_sender.assert_awaited_once_with(
            "terminal-1",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
        assert tmux_sender.await_count == 1
        assert tmux_sender.await_args is not None

    @pytest.mark.asyncio
    async def test_terminal_agent_uses_managed_terminal_when_session_name_missing(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """A bound child terminal wakes even when its legacy context has only a pane."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context={
                "tmux_pane": "%5",
                "tmux_socket_path": "/tmp/tmux-501/gobby",
            },
        )
        tmux_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is True
        assert result["method"] == "terminal"
        tmux_sender.assert_awaited_once_with(
            "terminal-1",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
        assert "Task completed" not in tmux_sender.await_args.args[1]

    @pytest.mark.asyncio
    async def test_terminal_agent_fallback_to_ism_when_tmux_fails(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Durable ISM remains when managed terminal wake fails."""
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
            terminal_manager=_managed_terminal(),
        )
        await dispatcher.wake(WAKE_SESSION_ID, "Pipeline completed", {"status": "completed"})

        ism_manager.create_message.assert_called_once()
        assert ism_manager.create_message.call_count == 1
        assert ism_manager.create_message.call_args is not None
        failing_tmux.assert_awaited_once_with(
            "terminal-1",
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
            terminal_manager=_managed_terminal(),
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
            "terminal-1",
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
    async def test_unbound_repaired_pane_keeps_durable_message_without_raw_wake(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """A repaired pane alone cannot receive an untracked terminal write."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_window_id": "@7", "tmux_pane": "%19"}',
        )
        tmux_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )

        result = await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed"})

        assert result["ism_persisted"] is True
        assert result["error_code"] == "no_live_wake_channel"
        ism_manager.create_message.assert_called_once()
        call_kwargs = ism_manager.create_message.call_args.kwargs
        assert call_kwargs["content"] == "Done"
        tmux_sender.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unbound_socket_path_does_not_authorize_raw_wake(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """A recorded socket does not establish live terminal ownership."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context={
                "tmux_pane": "%12",
                "tmux_socket_path": "/tmp/tmux-501/gobby",
            },
        )
        tmux_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )

        result = await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed"})

        assert result["ism_persisted"] is True
        assert result["error_code"] == "no_live_wake_channel"
        tmux_sender.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_managed_terminal_decline_returns_structured_result_without_warning(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A coordinator decline returns structured diagnostics without a traceback."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
        )
        tmux_sender = AsyncMock(
            side_effect=AutomaticWriteDeclined(
                AutomaticWriteQuarantined(action_key="wake:terminal-1")
            )
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
        )
        ledger = PendingWakeLedger(dispatcher)

        with caplog.at_level(logging.INFO, logger="gobby.events.wake"):
            result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is False
        assert result["method"] == "terminal"
        assert result["error_code"] == "automatic_write_quarantined"
        assert result["decline_reason"] == "automatic_write_quarantined"
        tmux_sender.assert_awaited_once()
        assert ledger.recorded == []
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
        tmux_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            lifecycle_refresh=lifecycle_refresh,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
        )

        with caplog.at_level(logging.WARNING, logger="gobby.events.wake"):
            result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)
            await drain_asyncio_tasks()

        assert result["skipped"] == "session_active"
        lifecycle_refresh.assert_awaited_once_with(WAKE_SESSION_ID)
        tmux_sender.assert_not_awaited()
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

        tmux_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            lifecycle_refresh=blocked_refresh,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
        )

        result = await asyncio.wait_for(dispatcher.dispatch_live_wake(WAKE_SESSION_ID), 0.5)
        assert result["delivered"] is True
        tmux_sender.assert_awaited_once()
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
        tmux_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["skipped"] == "session_active"
        tmux_sender.assert_not_awaited()

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

        async def send_terminal(*_args: object, **_kwargs: object) -> None:
            wake_delivered.set()

        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            lifecycle_refresh=blocked_refresh,
            tmux_sender=send_terminal,
            terminal_manager=_managed_terminal(),
        )

        result = await asyncio.wait_for(dispatcher.dispatch_live_wake(WAKE_SESSION_ID), 0.5)
        assert result["skipped"] == "session_active"
        await asyncio.wait_for(refresh_started.wait(), 0.5)
        release_refresh.set()
        await asyncio.wait_for(wake_delivered.wait(), 0.5)

    async def test_idle_claude_prompt_receives_deferred_wake(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """An active Claude row idle at its prompt is paused, then the retry delivers."""
        session = FakeSession(
            id=WAKE_SESSION_ID,
            terminal_context={"tmux_pane": "%12"},
            status="active",
            source="claude",
            updated_at=datetime(2026, 9, 23, 23, 14, tzinfo=UTC),
        )
        session_manager.get.return_value = session
        idle = _claude_idle_prompt()
        assert idle.composer.state == "empty"
        assert idle.turn_in_flight_fingerprint is None

        async def probe(_session: object, _terminal: object | None) -> TerminalActivity:
            return idle

        def pause(session_id: str, *, observed_updated_at: datetime) -> FakeSession:
            assert session_id == session.id
            assert observed_updated_at == session.updated_at
            session.status = "paused"
            return session

        session_manager._pause_idle_prompt_active = pause

        async def flush(_session_id: str) -> None:
            return None

        wake_delivered = asyncio.Event()

        async def send_terminal(*_args: object, **_kwargs: object) -> None:
            wake_delivered.set()

        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            lifecycle_refresh=flush,
            activity_probe=probe,
            tmux_sender=send_terminal,
            terminal_manager=_managed_terminal(),
        )

        result = await asyncio.wait_for(dispatcher.dispatch_live_wake(WAKE_SESSION_ID), 0.5)
        assert result["skipped"] == "session_active"
        await asyncio.wait_for(wake_delivered.wait(), 0.5)
        assert session.status == "paused"

    async def test_idle_codex_prompt_touched_after_restart_receives_deferred_wake(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = FakeSession(
            id=WAKE_SESSION_ID,
            terminal_context={"gobby_terminal_id": "terminal-1"},
            status="active",
            source="codex",
            updated_at=datetime(2026, 9, 27, 1, 29, tzinfo=UTC),
        )
        session_manager.get.return_value = session
        idle = _codex_idle_prompt()
        assert idle.composer.state == "empty"
        terminal = object()
        terminal_manager = MagicMock()
        terminal_manager.resolve_live_for_session.return_value = terminal
        probe_count = 0
        pause_count = 0

        async def probe(_session: object, observed_terminal: object | None) -> TerminalActivity:
            nonlocal probe_count
            assert observed_terminal is terminal
            probe_count += 1
            return idle

        def pause(session_id: str, *, observed_updated_at: datetime) -> FakeSession:
            nonlocal pause_count
            assert session_id == session.id
            assert observed_updated_at == session.updated_at
            pause_count += 1
            session.status = "paused"
            return session

        session_manager._pause_idle_prompt_active = pause

        async def flush(_session_id: str) -> None:
            return None

        delivered = asyncio.Event()

        async def send_terminal(*_args: object, **_kwargs: object) -> dict[str, object]:
            delivered.set()
            return {"session_id": session.id, "delivered": True, "method": "terminal"}

        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            lifecycle_refresh=flush,
            activity_probe=probe,
            terminal_manager=terminal_manager,
            tmux_sender=AsyncMock(),
        )
        dispatcher._restart_horizon_ms = int(
            datetime(2026, 9, 27, 1, 28, tzinfo=UTC).timestamp() * 1000
        )
        monkeypatch.setattr(dispatcher, "_send_managed_terminal_wake", send_terminal)

        result = await asyncio.wait_for(dispatcher.dispatch_live_wake(WAKE_SESSION_ID), 0.5)
        assert result["skipped"] == "session_active"
        await asyncio.wait_for(delivered.wait(), 0.5)
        assert session.status == "paused"
        assert (probe_count, pause_count) == (2, 1)

    @pytest.mark.parametrize("source", ["claude", "codex"])
    @pytest.mark.parametrize("kind", ["in_flight", "draft"])
    async def test_active_generation_or_draft_stays_active(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        source: str,
        kind: str,
    ) -> None:
        """A working turn or a typed draft is not paused by the deferred retry."""
        session = FakeSession(
            id=WAKE_SESSION_ID,
            terminal_context={"tmux_pane": "%12"},
            status="active",
            source=source,
            updated_at=datetime(2026, 9, 23, 23, 14, tzinfo=UTC),
        )
        session_manager.get.return_value = session
        if kind == "in_flight":
            activity = TerminalActivity(
                ComposerRead("empty"),
                turn_in_flight_fingerprint="turn-1",
            )
        else:
            activity = TerminalActivity(ComposerRead("draft"))

        async def probe(_session: object, _terminal: object | None) -> TerminalActivity:
            return activity

        def pause(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("idle-prompt pause must not run")

        session_manager._pause_idle_prompt_active = pause

        async def flush(_session_id: str) -> None:
            return None

        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            lifecycle_refresh=flush,
            activity_probe=probe,
        )

        result = await asyncio.wait_for(dispatcher.dispatch_live_wake(WAKE_SESSION_ID), 0.5)
        assert result["skipped"] == "session_active"
        refresh = dispatcher._deferred_refreshes[WAKE_SESSION_ID]
        await asyncio.wait_for(refresh, 0.5)
        assert session.status == "active"

    @pytest.mark.asyncio
    async def test_interactive_session_without_binding_reports_no_live_channel(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Terminal context without a live binding gets a precise diagnostic."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context={"parent_pid": 12345},
        )
        tmux_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
        )

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is False
        assert result["method"] is None
        assert result["error_code"] == "no_live_wake_channel"
        tmux_sender.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_interactive_session_without_sender_reports_no_live_channel(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """A recorded pane without binding or sender has no live channel."""
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
        assert result["method"] is None
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
    async def test_terminal_wake_coalesces_during_idle_window(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Bursty completions during one idle turn send one wake, every ISM stored."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )
        tmux_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
        )
        ledger = PendingWakeLedger(dispatcher)

        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r1"})
        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r2"})
        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r3"})

        tmux_sender.assert_awaited_once_with(
            "terminal-1",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
        assert ism_manager.create_message.call_count == 3
        assert ledger.recorded == [WAKE_SESSION_ID]

    @pytest.mark.asyncio
    async def test_concurrent_terminal_wakes_coalesce_before_sending_text(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Concurrent completions must not interleave duplicate wake prompts."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )

        async def slow_terminal_send(
            _terminal_id: str,
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
        tmux_sender = AsyncMock(side_effect=slow_terminal_send)
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
        )
        ledger = PendingWakeLedger(dispatcher)

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
            "terminal-1",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
        assert ism_manager.create_message.call_count == 3
        assert ledger.recorded == [WAKE_SESSION_ID]

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
            terminal_manager=_managed_terminal(),
        )
        ledger = PendingWakeLedger(dispatcher)

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
            "terminal-1",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )
        assert ism_manager.create_message.call_count == 3
        assert ledger.recorded == [WAKE_SESSION_ID]

    @pytest.mark.asyncio
    async def test_terminal_wake_resumes_after_turn_advances(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
    ) -> None:
        """Once the user takes a new turn, the next completion wakes again."""
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

        tmux_sender = AsyncMock()
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
        )

        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r1"})
        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r2"})

        assert tmux_sender.await_count == 2

    @pytest.mark.asyncio
    async def test_terminal_wake_decline_does_not_record_timestamp(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Coordinator declines stay retryable and do not emit warning traces."""
        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=0,
            terminal_context='{"tmux_pane": "%12"}',
            turn_count=5,
        )
        tmux_sender = AsyncMock(
            side_effect=[
                AutomaticWriteDeclined(AutomaticWriteQuarantined(action_key="wake:terminal-1")),
                None,
            ]
        )
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
        )
        ledger = PendingWakeLedger(dispatcher)

        with caplog.at_level(logging.INFO, logger="gobby.events.wake"):
            await dispatcher.wake(
                WAKE_SESSION_ID,
                "Done",
                {"status": "completed", "run_id": "r1"},
            )

        assert ledger.recorded == []
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
        assert not [record for record in caplog.records if record.exc_info]

        await dispatcher.wake(WAKE_SESSION_ID, "Done", {"status": "completed", "run_id": "r2"})

        assert tmux_sender.await_count == 2

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
        ledger = PendingWakeLedger(dispatcher)

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
        assert ledger.recorded == ["web-1"]

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

    @pytest.mark.asyncio
    async def test_provider_retry_bypasses_debounce_but_preserves_draft_guard(
        self,
        session_manager: MagicMock,
        ism_manager: MagicMock,
        tmux_sender: AsyncMock,
    ) -> None:
        from gobby.agents.idle_detector import ComposerRead
        from gobby.events.live_wake import TerminalActivity

        session_manager.get.return_value = FakeSession(
            id=WAKE_SESSION_ID,
            agent_depth=1,
            terminal_context={"tmux_session": "gobby-agent-abc"},
            status="paused",
        )
        probe = AsyncMock(return_value=TerminalActivity(ComposerRead("empty", None)))
        dispatcher = WakeDispatcher(
            session_manager=session_manager,
            ism_manager=ism_manager,
            tmux_sender=tmux_sender,
            terminal_manager=_managed_terminal(),
            activity_probe=probe,
        )
        ledger = PendingWakeLedger(dispatcher)

        first = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)
        debounced = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)
        retry_prompt = "Continue the interrupted work."
        retried = await dispatcher.wake(
            WAKE_SESSION_ID,
            retry_prompt,
            {"message_type": "provider_error_resume", "completion_id": "failed-turn-1"},
            bypass_debounce=True,
            prompt=retry_prompt,
        )

        assert first["delivered"] is True
        assert debounced["delivered"] is False
        assert debounced["skipped"] == "debounced"
        assert retried["delivered"] is True
        assert tmux_sender.await_count == 2
        assert tmux_sender.await_args.args[1] == retry_prompt
        assert ledger.recorded == [WAKE_SESSION_ID, WAKE_SESSION_ID]
        assert ism_manager.create_message.call_args.kwargs["content"] == retry_prompt

        probe.return_value = TerminalActivity(ComposerRead("draft", "operator draft"))
        guarded = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID, bypass_debounce=True)

        assert guarded["delivered"] is False
        assert guarded["skipped"] == "composer_occupied"
        assert tmux_sender.await_count == 2


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
        terminal_manager.resolve_live_for_session.return_value = MagicMock(id="terminal-1")
        return WakeDispatcher(
            session_manager=session_manager,
            ism_manager=MagicMock(),
            tmux_sender=pane_sender,
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
        ledger = PendingWakeLedger(dispatcher)

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result == {
            "session_id": WAKE_SESSION_ID,
            "delivered": False,
            "method": "terminal",
            "skipped": "composer_occupied",
            "decline_reason": "composer_occupied",
            "ism_persisted": True,
        }
        pane_sender.assert_not_awaited()
        assert ledger.recorded == []

    @pytest.mark.asyncio
    async def test_urgent_wake_defers_when_the_composer_holds_a_draft(self) -> None:
        from gobby.agents.idle_detector import ComposerRead
        from gobby.events.live_wake import TerminalActivity, composer_occupied_result

        pane_sender = AsyncMock()
        probe = AsyncMock(return_value=TerminalActivity(ComposerRead("draft", "hello draft")))
        dispatcher = self._dispatcher(probe, pane_sender)

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID, priority="urgent")

        assert result == composer_occupied_result(WAKE_SESSION_ID, method="terminal")
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
            "terminal-1",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
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
            "terminal-1",
            CONTINUE_WAKE_MESSAGE,
            submit=True,
            clear_before_submit=True,
            cli_source=ANY,
        )

    @pytest.mark.asyncio
    async def test_probe_error_drains_blind(self) -> None:
        pane_sender = AsyncMock()
        dispatcher = self._dispatcher(AsyncMock(side_effect=RuntimeError("no pane")), pane_sender)

        result = await dispatcher.dispatch_live_wake(WAKE_SESSION_ID)

        assert result["delivered"] is True
        pane_sender.assert_awaited_once()
