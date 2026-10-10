"""Queued Claude prompts fence turn ends even when no submit hook fired."""

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.events.wake import WakeDispatcher
from gobby.events.wake_active_recovery import reconcile_idle_prompt_session
from gobby.events.wake_recovery import WakeReplayCoordinator
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_types import HookSessionManager
from gobby.runner_init.wake_activity import probe_terminal_activity
from gobby.sessions.turn_lifecycle import TurnDisposition, TurnEvidence, TurnLifecycleReducer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.terminals.composer_ledger import ComposerLedger, LedgerRead, UnsafeReason
from gobby.terminals.host_events import InputActivityEvent
from tests._timing import drain_asyncio_tasks
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit


@dataclass
class QueuedSeat:
    sessions: SessionManager
    lifecycle: TurnLifecycleReducer
    session_id: str
    queued_at: datetime
    transcript: Path


@pytest.fixture
def queued_seat(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> QueuedSeat:
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="queued-turn",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
    )
    started = datetime.now(UTC) - timedelta(seconds=10)
    queued_at = started + timedelta(seconds=2)
    transcript = tmp_path / "queued.jsonl"
    records = [
        {
            "type": "user",
            "promptId": "original-prompt",
            "promptSource": "typed",
            "timestamp": started.isoformat(),
        },
        {
            "type": "user",
            "promptId": "queued-prompt",
            "interruptedMessageId": "interrupted",
            "timestamp": (queued_at - timedelta(milliseconds=1)).isoformat(),
        },
        {
            "type": "user",
            "timestamp": queued_at.isoformat(),
            "promptId": "queued-prompt",
            "promptSource": "queued",
            "message": {"role": "user", "content": "replacement instruction"},
        },
    ]
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    sessions.update(session.id, transcript_path=str(transcript))
    lifecycle = TurnLifecycleReducer(sessions)
    lifecycle.begin_turn(
        session.id,
        TurnEvidence(source="claude", provider_turn_key="original-prompt", event_time=started),
    )
    return QueuedSeat(sessions, lifecycle, session.id, queued_at, transcript)


@pytest.mark.parametrize("ending_key", ["queued-prompt", "original-prompt"])
def test_queued_prompt_stop_uses_transcript_order(
    queued_seat: QueuedSeat,
    ending_key: str,
) -> None:
    result = queued_seat.lifecycle.end_turn(
        queued_seat.session_id,
        "completed",
        TurnEvidence(
            source="claude",
            provider_turn_key=ending_key,
            event_time=queued_seat.queued_at + timedelta(seconds=1),
        ),
    )
    current = queued_seat.sessions.get(queued_seat.session_id)
    assert current is not None
    if ending_key == "queued-prompt":
        assert result.applied
        assert current.status == "paused"
        assert result.lifecycle.provider_turn_key == "queued-prompt"
        assert result.lifecycle.generation == 2
    else:
        assert not result.applied
        assert current.status == "active"
        assert result.reason == "stale_provider_turn"


def test_queued_prompt_tool_event_persists_adopted_generation(queued_seat: QueuedSeat) -> None:
    result = queued_seat.lifecycle.resumed_work(
        queued_seat.session_id,
        TurnEvidence(
            source="claude",
            provider_turn_key="queued-prompt",
            event_time=queued_seat.queued_at + timedelta(seconds=1),
        ),
    )
    assert result.applied
    stored = queued_seat.lifecycle.get(queued_seat.session_id)
    assert stored.provider_turn_key == "queued-prompt"
    assert stored.generation == 2
    assert stored.turn_state == "open"


@pytest.mark.parametrize(
    "case",
    [
        "missing-timestamp",
        "empty-timestamp",
        "older-timestamp",
        "invalid-timestamp",
        "typed",
        "queue-operation",
        "later-typed",
        "foreign-provider",
        "explicit-generation",
        "future",
    ],
)
def test_unproven_queued_prompt_cannot_end_turn(queued_seat: QueuedSeat, case: str) -> None:
    seat = queued_seat
    record: dict[str, Any] = {
        "type": "user",
        "promptId": "queued-prompt",
        "promptSource": "queued",
        "timestamp": seat.queued_at.isoformat(),
    }
    if case == "missing-timestamp":
        del record["timestamp"]
    elif case == "empty-timestamp":
        record["timestamp"] = ""
    elif case == "older-timestamp":
        record["timestamp"] = (seat.queued_at - timedelta(seconds=10)).isoformat()
    elif case == "invalid-timestamp":
        record["timestamp"] = "invalid"
    elif case == "typed":
        record["promptSource"] = "typed"
    elif case == "queue-operation":
        record["type"] = "queue-operation"
    elif case == "future":
        record["timestamp"] = (seat.queued_at + timedelta(seconds=2)).isoformat()
    seat.transcript.write_text(json.dumps(record) + "\n")
    if case == "later-typed":
        with seat.transcript.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "user",
                        "promptSource": "typed",
                        "promptId": "later-prompt",
                        "timestamp": (seat.queued_at + timedelta(seconds=1)).isoformat(),
                    }
                )
                + "\n"
            )
    result = seat.lifecycle.end_turn(
        seat.session_id,
        "completed",
        TurnEvidence(
            source="codex" if case == "foreign-provider" else "claude",
            generation=1 if case == "explicit-generation" else None,
            provider_turn_key="queued-prompt",
            event_time=seat.queued_at + timedelta(seconds=1),
        ),
    )
    assert not result.applied
    current = seat.sessions.get(seat.session_id)
    assert current is not None and current.status == "active"
    assert seat.lifecycle.get(seat.session_id).provider_turn_key == "original-prompt"


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.mark.parametrize("reason", ["gap", "provider_limit"])
def test_queued_recovery_preserves_a_stronger_existing_block(reason: UnsafeReason) -> None:
    ledger = ComposerLedger()
    ledger.record_spawn("terminal", "host")
    ledger.block("terminal", reason)
    ledger.observe_write("terminal", origin="operator", kind="key", payload="escape")
    ledger.record_queued_submit("terminal", consumed_at=datetime.now(UTC) + timedelta(seconds=1))
    assert ledger.read("terminal") == LedgerRead("blocked", reason)


@pytest.mark.parametrize("disposition", ["completed", "user_interrupted"])
def test_queued_turn_keeps_a_later_interrupt_block(
    queued_seat: QueuedSeat,
    monkeypatch: pytest.MonkeyPatch,
    disposition: TurnDisposition,
) -> None:
    seat = queued_seat
    terminal = replace(make_memory_terminal(backend="native"), session_id=seat.session_id)
    ledger = ComposerLedger()
    ledger.record_spawn(terminal.id, "host")
    ledger.observe_write(terminal.id, origin="operator", kind="key", payload="escape")
    handlers = EventHandlers(session_manager=cast(HookSessionManager, seat.sessions))
    monkeypatch.setattr(handlers, "_composer_terminal", lambda _session_id: (ledger, terminal))
    response = handlers.handle_stop(
        HookEvent(
            event_type=HookEventType.STOP,
            session_id="queued-turn",
            source=SessionSource.CLAUDE,
            timestamp=seat.queued_at + timedelta(seconds=1),
            data={},
            provider_turn_key="queued-prompt",
            turn_disposition=disposition,
            metadata={"_platform_session_id": seat.session_id},
        )
    )
    assert response.decision == "allow"
    current = seat.sessions.get(seat.session_id)
    assert current is not None
    assert current.status == ("paused" if disposition == "completed" else "interrupted")
    assert ledger.read(terminal.id) == LedgerRead("blocked", "interrupt")


@pytest.mark.asyncio
@pytest.mark.parametrize("draft_position", ["none", "before-interrupt", "after-interrupt"])
async def test_queued_stop_recovers_native_composer_and_pending_wake(
    queued_seat: QueuedSeat,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    draft_position: str,
) -> None:
    seat = queued_seat
    terminal = replace(make_memory_terminal(backend="native"), session_id=seat.session_id)
    terminals = MemoryTerminalStore(terminal)
    ledger = ComposerLedger()
    ledger.record_spawn(terminal.id, "host")
    monkeypatch.setattr(
        "gobby.terminals.composer_ledger.utc_now",
        lambda: seat.queued_at - timedelta(milliseconds=1),
    )
    if draft_position == "before-interrupt":
        ledger.observe_host_event(
            InputActivityEvent(
                terminal_id=terminal.id,
                host_terminal_id="host-terminal",
                attachment_id="attachment",
                kind="input",
                bytes=5,
                epoch="host",
                seq=1,
                submit=False,
                interrupt=None,
            )
        )
    ledger.observe_host_event(
        InputActivityEvent(
            terminal_id=terminal.id,
            host_terminal_id="host-terminal",
            attachment_id="attachment",
            kind="input",
            bytes=1,
            epoch="host",
            seq=2,
            submit=False,
            interrupt="esc",
        )
    )
    if draft_position == "after-interrupt":
        ledger.observe_host_event(
            InputActivityEvent(
                terminal_id=terminal.id,
                host_terminal_id="host-terminal",
                attachment_id="attachment",
                kind="input",
                bytes=5,
                epoch="host",
                seq=3,
                submit=False,
                interrupt=None,
            )
        )
    monkeypatch.setattr("gobby.terminals.composer_ledger._bound", ledger)
    monkeypatch.setattr(
        "gobby.hooks.event_handlers._base.get_app_context",
        lambda: SimpleNamespace(
            write_coordinator=SimpleNamespace(composer_ledger=ledger),
            shutdown_in_progress=False,
        ),
    )
    handlers = EventHandlers(
        session_manager=cast(HookSessionManager, seat.sessions),
        terminal_manager=cast(TerminalManager, terminals),
    )
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id="queued-turn",
        source=SessionSource.CLAUDE,
        timestamp=seat.queued_at + timedelta(seconds=1),
        data={},
        provider_turn_key="queued-prompt",
        turn_disposition="completed",
        metadata={"_platform_session_id": seat.session_id},
    )
    messages = InterSessionMessageManager(temp_db)
    messages.create_message(
        from_session=seat.session_id,
        to_session=seat.session_id,
        content="pending Telegram DM",
        metadata_json='{"wake_requested": true}',
    )
    sender = AsyncMock(return_value=None)
    dispatcher = WakeDispatcher(
        session_manager=seat.sessions,
        ism_manager=messages,
        tmux_sender=sender,
        terminal_manager=terminals,
        run_db=_run_db,
        activity_probe=probe_terminal_activity,
    )
    coordinator = WakeReplayCoordinator(
        session_manager=seat.sessions,
        message_manager=messages,
        dispatcher=dispatcher,
        run_db=_run_db,
    )
    coordinator.bind_owner_loop(asyncio.get_running_loop())
    await coordinator.open()
    sender.assert_not_awaited()
    try:
        handlers.handle_stop(event)
        with seat.transcript.open("a") as stream:
            stream.write(json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
        session = seat.sessions.get(seat.session_id)
        assert session is not None and session.status == "paused"
        activity = await probe_terminal_activity(session, terminal)
        assert activity.composer.state == ("empty" if draft_position == "none" else "draft")
        await drain_asyncio_tasks(cycles=10)
        await coordinator.close()
        if draft_position != "none":
            sender.assert_not_awaited()
        else:
            sender.assert_awaited_once()
            assert sender.await_args is not None
            assert sender.await_args.kwargs["composer_confirmed_empty"] is True
            seat.sessions.update_session_status(seat.session_id, "active")
            observed = seat.sessions.get(seat.session_id)
            assert observed is not None
            reason = await reconcile_idle_prompt_session(
                session_manager=seat.sessions,
                observed=observed,
                terminal=terminal,
                activity_probe=probe_terminal_activity,
                run_db=_run_db,
            )
            assert reason == "paused"
            paused = seat.sessions.get(seat.session_id)
            assert paused is not None and paused.status == "paused"
    finally:
        await coordinator.close()
        retries = list(dispatcher._composer_retries.values())
        for retry in retries:
            retry.cancel()
        await asyncio.gather(*retries, return_exceptions=True)
