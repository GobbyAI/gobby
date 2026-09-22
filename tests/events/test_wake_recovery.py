"""Durable live-wake replay at committed turn boundaries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake import WakeDispatcher
from gobby.events.wake_active_recovery import (
    reconcile_restart_stale_session,
    restart_activity_allows_recovery,
    session_precedes_restart_horizon,
)
from gobby.events.wake_recovery import WakeReplayCoordinator
from gobby.hooks.receipt_effects import apply_acknowledged_receipt
from gobby.sessions.mailbox import MailboxService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from tests._timing import drain_asyncio_tasks
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


def _session(session_manager: SessionManager, project_id: str, external_id: str) -> str:
    return session_manager.register(
        external_id=external_id,
        machine_id=None,
        source="codex",
        project_id=project_id,
        title=external_id,
    ).id


def _pending_wake(
    manager: InterSessionMessageManager,
    sender_id: str,
    recipient_id: str,
) -> str:
    return manager.create_message(
        from_session=sender_id,
        to_session=recipient_id,
        content="pending wake",
        metadata_json='{"wake_requested": true}',
    ).id


def _set_lifecycle_time(
    session_manager: SessionManager,
    session_id: str,
    value: datetime | None,
) -> None:
    with session_manager.db.transaction():
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = %s, last_activity = %s WHERE id = %s",
            (value, value, session_id),
        )


async def _empty_activity(_session: object, _terminal: object | None) -> TerminalActivity:
    return TerminalActivity(ComposerRead("empty"))


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        self.calls.append((session_id, priority))
        return {"session_id": session_id, "delivered": True, "method": "fake"}


@pytest.mark.parametrize("composer_state", ["empty", "draft", "unknown"])
@pytest.mark.parametrize("in_flight", [False, True])
def test_restart_activity_snapshot_predicate_matrix(
    composer_state: str,
    in_flight: bool,
) -> None:
    activity = TerminalActivity(
        ComposerRead(cast(Any, composer_state)),
        "fingerprint" if in_flight else None,
    )

    assert restart_activity_allows_recovery(activity) is (
        not in_flight and composer_state in {"empty", "draft"}
    )


@pytest.mark.asyncio
async def test_restart_horizon_and_final_recheck_matrix(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session_id = _session(session_manager, sample_project["id"], "horizon-matrix")
    horizon = datetime(2026, 9, 21, 6, 0, tzinfo=UTC)
    horizon_ms = int(horizon.timestamp() * 1_000)
    session = session_manager.get(session_id)
    assert session is not None
    before = horizon - timedelta(milliseconds=1)
    after = horizon + timedelta(milliseconds=1)

    assert not session_precedes_restart_horizon(replace(session, last_activity=None), horizon_ms)
    assert not session_precedes_restart_horizon(
        SimpleNamespace(
            status="active",
            session_type="terminal",
            last_activity=horizon,
            updated_at=None,
        ),
        horizon_ms,
    )
    assert session_precedes_restart_horizon(
        replace(session, last_activity=before, updated_at=before), horizon_ms
    )
    assert session_precedes_restart_horizon(
        replace(session, last_activity=horizon, updated_at=horizon), horizon_ms
    )
    assert not session_precedes_restart_horizon(
        replace(session, last_activity=after, updated_at=horizon), horizon_ms
    )
    assert not session_precedes_restart_horizon(
        replace(session, last_activity=horizon, updated_at=after), horizon_ms
    )

    _set_lifecycle_time(session_manager, session_id, before)
    observed = session_manager.get(session_id)
    assert observed is not None
    calls = 0

    async def raced_snapshot(_session: object, _terminal: object | None) -> TerminalActivity:
        nonlocal calls
        calls += 1
        if calls == 2:
            return TerminalActivity(ComposerRead("empty"), "turn-started")
        return TerminalActivity(ComposerRead("empty"))

    assert (
        await reconcile_restart_stale_session(
            session_manager=session_manager,
            observed=observed,
            terminal=None,
            activity_probe=raced_snapshot,
            run_db=_run_db,
            restart_horizon_ms=horizon_ms,
            excluded_session_ids=frozenset(),
        )
        is None
    )
    still_active = session_manager.get(session_id)
    assert still_active is not None and still_active.status == "active"

    calls = 0

    async def changed_row(_session: object, _terminal: object | None) -> TerminalActivity:
        nonlocal calls
        calls += 1
        if calls == 2:
            with session_manager.db.transaction():
                session_manager.db.execute(
                    "UPDATE sessions SET updated_at = %s WHERE id = %s",
                    (before + timedelta(microseconds=1), session_id),
                )
        return TerminalActivity(ComposerRead("empty"))

    assert (
        await reconcile_restart_stale_session(
            session_manager=session_manager,
            observed=observed,
            terminal=None,
            activity_probe=changed_row,
            run_db=_run_db,
            restart_horizon_ms=horizon_ms,
            excluded_session_ids=frozenset(),
        )
        is None
    )
    current = session_manager.get(session_id)
    assert current is not None and current.status == "active"


@pytest.mark.asyncio
async def test_gate_holds_committed_transition_until_ready(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    sender_id = _session(session_manager, sample_project["id"], "sender")
    recipient_id = _session(session_manager, sample_project["id"], "recipient")
    messages = InterSessionMessageManager(temp_db)
    _pending_wake(messages, sender_id, recipient_id)
    dispatcher = _RecordingDispatcher()
    coordinator = WakeReplayCoordinator(
        message_manager=messages,
        session_manager=session_manager,
        dispatcher=dispatcher,
        run_db=_run_db,
    )
    coordinator.bind_owner_loop(asyncio.get_running_loop())

    session_manager.update_status(recipient_id, "paused")
    await drain_asyncio_tasks()
    assert dispatcher.calls == []

    await coordinator.open()

    assert dispatcher.calls == [(recipient_id, "normal")]
    assert messages.get_message(messages.get_undelivered_messages(recipient_id)[0].id)


class _ControlledDispatcher:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.outcomes: list[dict[str, Any] | BaseException] = []

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_replay_task_ownership_and_retry_matrix(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    sender_id = _session(session_manager, sample_project["id"], "sender")
    recipient_id = _session(session_manager, sample_project["id"], "recipient")
    messages = InterSessionMessageManager(temp_db)
    dispatcher = _ControlledDispatcher()
    coordinator = WakeReplayCoordinator(
        message_manager=messages,
        session_manager=session_manager,
        dispatcher=dispatcher,
        run_db=_run_db,
    )
    coordinator.bind_owner_loop(asyncio.get_running_loop())
    await coordinator.open()
    _pending_wake(messages, sender_id, recipient_id)

    dispatcher.started.clear()
    dispatcher.release.clear()
    dispatcher.outcomes.append({"session_id": recipient_id, "delivered": True, "method": "fake"})
    joiner = asyncio.create_task(coordinator.request_replay(recipient_id))
    await dispatcher.started.wait()
    joiner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await joiner
    dispatcher.release.set()
    await drain_asyncio_tasks()
    assert dispatcher.calls == 1

    dispatcher.started.clear()
    dispatcher.release.set()
    dispatcher.outcomes.append(RuntimeError("dispatch failed"))
    with pytest.raises(RuntimeError, match="dispatch failed"):
        await coordinator.request_replay(recipient_id)

    dispatcher.outcomes.append(
        {
            "session_id": recipient_id,
            "delivered": False,
            "method": "fake",
            "decline_reason": "composer_occupied",
        }
    )
    declined = await coordinator.request_replay(recipient_id)
    assert declined is not None
    assert declined["decline_reason"] == "composer_occupied"

    dispatcher.started.clear()
    dispatcher.release.clear()
    dispatcher.outcomes.append({"session_id": recipient_id, "delivered": True, "method": "fake"})
    owned_joiner = asyncio.create_task(coordinator.request_replay(recipient_id))
    await dispatcher.started.wait()
    owned = coordinator._tasks[recipient_id]
    owned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owned_joiner
    dispatcher.release.set()
    await drain_asyncio_tasks()

    dispatcher.outcomes.append({"session_id": recipient_id, "delivered": True, "method": "fake"})
    retried = await coordinator.request_replay(recipient_id)
    assert retried is not None and retried["delivered"] is True
    assert messages.get_undelivered_wake_messages(recipient_id)


class _TurnDispatcher:
    def __init__(self, session_manager: SessionManager, clock: list[int]) -> None:
        self._sessions = session_manager
        self._clock = clock
        self.calls: list[tuple[int, str]] = []

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        assert session is not None
        self.calls.append((self._clock[0], session.status))
        if session.status == "active":
            return {
                "session_id": session_id,
                "delivered": False,
                "method": "next_call_context",
                "skipped": "session_active",
                "decline_reason": "session_active",
            }
        if len(self.calls) == 1:
            self._sessions.update_status(session_id, "active")
        return {"session_id": session_id, "delivered": True, "method": "fake"}


@pytest.mark.asyncio
async def test_two_mid_turn_messages_replay_through_acknowledged_receipt(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sender_id = _session(session_manager, sample_project["id"], "sender")
    recipient_id = _session(session_manager, sample_project["id"], "recipient")
    session_manager.update_status(recipient_id, "paused")
    messages = InterSessionMessageManager(temp_db)
    clock = [0]
    dispatcher = _TurnDispatcher(session_manager, clock)
    mailbox = MailboxService(
        db=temp_db,
        message_manager=messages,
        session_manager=session_manager,
        wake_dispatcher=dispatcher,
    )
    coordinator = WakeReplayCoordinator(
        message_manager=messages,
        session_manager=session_manager,
        dispatcher=dispatcher,
        run_db=_run_db,
    )
    coordinator.bind_owner_loop(asyncio.get_running_loop())
    await coordinator.open()

    first = await mailbox.send(
        from_session_id=sender_id,
        target="session",
        target_id=recipient_id,
        content="first",
        wake=True,
    )
    clock[0] += 5
    with caplog.at_level(logging.INFO, logger="gobby.sessions.mailbox_delivery"):
        second = await mailbox.send(
            from_session_id=sender_id,
            target="session",
            target_id=recipient_id,
            content="second",
            wake=True,
        )

    assert dispatcher.calls[:2] == [(0, "paused"), (5, "active")]
    assert second.wake_results[0]["message_id"] == second.message_ids[0]
    assert second.wake_results[0]["decline_reason"] == "session_active"
    decline_logs = [
        record.getMessage() for record in caplog.records if "declined" in record.getMessage()
    ]
    assert decline_logs == [
        f"mailbox wake declined for session {recipient_id} message "
        f"{second.message_ids[0]}: session_active"
    ]

    session_manager.update_status(recipient_id, "paused")
    await drain_asyncio_tasks(cycles=10)
    assert dispatcher.calls == [(0, "paused"), (5, "active"), (5, "paused")]

    pending = messages.get_undelivered_messages(recipient_id)
    assert [message.id for message in pending] == first.message_ids + second.message_ids
    apply_acknowledged_receipt(
        SimpleNamespace(
            receipt_id="receipt",
            staged_payload={
                "pending_message_ids": [message.id for message in pending],
                "pending_message_session_id": recipient_id,
            },
        ),
        message_manager=messages,
    )
    delivered = [
        messages.get_message(message_id) for message_id in first.message_ids + second.message_ids
    ]
    assert all(message is not None and message.delivered_at is not None for message in delivered)


@pytest.mark.asyncio
async def test_restart_reconciliation_reaches_delivered_without_manual_input(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    sender_id = _session(session_manager, sample_project["id"], "restart-sender")
    recipient_id = _session(session_manager, sample_project["id"], "restart-recipient")
    session_manager.update_status(sender_id, "paused")
    horizon = datetime.now(UTC)
    _set_lifecycle_time(session_manager, recipient_id, horizon - timedelta(seconds=1))
    messages = InterSessionMessageManager(temp_db)
    message_id = _pending_wake(messages, sender_id, recipient_id)
    terminal = replace(make_memory_terminal(backend="native"), session_id=recipient_id)
    terminals = MemoryTerminalStore(terminal)
    native_sender = AsyncMock()
    pane_sender = AsyncMock()
    dispatcher = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=messages,
        tmux_sender=native_sender,
        tmux_pane_sender=pane_sender,
        terminal_manager=terminals,
        run_db=_run_db,
        activity_probe=_empty_activity,
    )
    coordinator = WakeReplayCoordinator(
        message_manager=messages,
        session_manager=session_manager,
        dispatcher=dispatcher,
        run_db=_run_db,
    )
    coordinator.bind_owner_loop(asyncio.get_running_loop())

    paused = await dispatcher.reconcile_restart_active_sessions(
        restart_horizon_ms=int(horizon.timestamp() * 1_000),
        excluded_session_ids=frozenset(),
        recovery_safe=True,
    )
    await coordinator.open()

    assert paused == (recipient_id,)
    native_sender.assert_awaited_once()
    assert native_sender.await_args is not None
    assert native_sender.await_args.args[0] == terminal.id
    pane_sender.assert_not_awaited()
    apply_acknowledged_receipt(
        SimpleNamespace(
            receipt_id="restart-receipt",
            staged_payload={
                "pending_message_ids": [message_id],
                "pending_message_session_id": recipient_id,
            },
        ),
        message_manager=messages,
    )
    delivered = messages.get_message(message_id)
    assert delivered is not None and delivered.delivered_at is not None


class _SchedulingFailureLoop:
    def is_running(self) -> bool:
        return True

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, _callback: object, *_args: object) -> None:
        raise RuntimeError("loop stopped")


class _CancelableDispatcher:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = True
        self.attempts = 0
        self.injections = 0

    async def dispatch_live_wake(
        self,
        session_id: str,
        *,
        priority: str = "normal",
    ) -> dict[str, Any]:
        self.attempts += 1
        self.started.set()
        if self.hold:
            await self.release.wait()
        self.injections += 1
        return {"session_id": session_id, "delivered": True, "method": "terminal"}


@pytest.mark.asyncio
async def test_committed_reconciliation_survives_callback_and_task_failure(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    sender_id = _session(session_manager, sample_project["id"], "failure-sender")
    first_id = _session(session_manager, sample_project["id"], "schedule-failure")
    horizon = datetime.now(UTC)
    before = horizon - timedelta(seconds=1)
    _set_lifecycle_time(session_manager, first_id, before)
    messages = InterSessionMessageManager(temp_db)
    first_message_id = _pending_wake(messages, sender_id, first_id)
    recording = _RecordingDispatcher()
    coordinator = WakeReplayCoordinator(
        message_manager=messages,
        session_manager=session_manager,
        dispatcher=recording,
        run_db=_run_db,
    )
    coordinator.bind_owner_loop(cast(asyncio.AbstractEventLoop, _SchedulingFailureLoop()))
    coordinator._ready = True
    observed = session_manager.get(first_id)
    assert observed is not None

    reconciled = await reconcile_restart_stale_session(
        session_manager=session_manager,
        observed=observed,
        terminal=None,
        activity_probe=_empty_activity,
        run_db=_run_db,
        restart_horizon_ms=int(horizon.timestamp() * 1_000),
        excluded_session_ids=frozenset(),
    )
    assert reconciled is not None and reconciled.status == "paused"
    coordinator.bind_owner_loop(asyncio.get_running_loop())
    await coordinator.open()
    assert recording.calls == [(first_id, "normal")]
    apply_acknowledged_receipt(
        SimpleNamespace(
            receipt_id="schedule-receipt",
            staged_payload={
                "pending_message_ids": [first_message_id],
                "pending_message_session_id": first_id,
            },
        ),
        message_manager=messages,
    )

    second_id = _session(session_manager, sample_project["id"], "owner-cancelled")
    _set_lifecycle_time(session_manager, second_id, before)
    _pending_wake(messages, sender_id, second_id)
    cancelable = _CancelableDispatcher()
    coordinator._dispatcher = cancelable
    observed = session_manager.get(second_id)
    assert observed is not None
    reconciled = await reconcile_restart_stale_session(
        session_manager=session_manager,
        observed=observed,
        terminal=None,
        activity_probe=_empty_activity,
        run_db=_run_db,
        restart_horizon_ms=int(horizon.timestamp() * 1_000),
        excluded_session_ids=frozenset(),
    )
    assert reconciled is not None
    await cancelable.started.wait()
    owned = coordinator._tasks[second_id]
    owned.cancel()
    await drain_asyncio_tasks()

    cancelable.hold = False
    await coordinator.open()
    assert cancelable.attempts == 2
    assert cancelable.injections == 1
