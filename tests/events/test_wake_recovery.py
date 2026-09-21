"""Durable live-wake replay at committed turn boundaries."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.events.wake_recovery import WakeReplayCoordinator
from gobby.hooks.receipt_effects import apply_acknowledged_receipt
from gobby.sessions.mailbox import MailboxService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from tests._timing import drain_asyncio_tasks

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


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        self.calls.append((session_id, priority))
        return {"session_id": session_id, "delivered": True, "method": "fake"}


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
