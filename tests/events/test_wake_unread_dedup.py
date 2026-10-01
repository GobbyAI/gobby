"""At most one unread live wake per session until the mailbox is read (#23125).

A delivered wake stays outstanding while it is fresh, mail sent by its attempt
is unread, and nothing has been read since. Later messages queue durably
without another live wake; any read consumes the wake and the next message may
wake again. The outstanding wake is stored with the session, so a fresh
dispatcher keeps it.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from gobby.events.wake import LIVE_WAKE_FRESH_SECONDS, WakeDispatcher
from gobby.sessions.mailbox import MailboxService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.datetime import utc_now
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit


class RecordingSender:
    """Terminal wake sender that records submissions and can fail once."""

    def __init__(self, *, fail_first: bool = False) -> None:
        self.submitted: list[str] = []
        self._fail_next = fail_first

    async def __call__(
        self,
        identity: str,
        message: str,
        *,
        submit: bool = False,
        clear_before_submit: bool = False,
        cli_source: str | None = None,
    ) -> None:
        await asyncio.sleep(0)
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("terminal write failed")
        self.submitted.append(identity)


def _register(session_manager: SessionManager, project_id: str, external_id: str) -> Session:
    return session_manager.register(
        external_id=external_id,
        machine_id=None,
        source="codex",
        project_id=project_id,
        title=external_id,
    )


class Harness:
    def __init__(
        self, temp_db: HubDatabase, session_manager: SessionManager, project_id: str
    ) -> None:
        self.db = temp_db
        self.session_manager = session_manager
        self.messages = InterSessionMessageManager(temp_db)
        self.sender_session = _register(session_manager, project_id, "sender")
        self.recipient = _register(session_manager, project_id, "recipient").id
        self.terminals = MemoryTerminalStore()
        terminal = replace(
            make_memory_terminal(backend="native"),
            session_id=self.recipient,
            project_id=project_id,
        )
        self.terminals.rows[terminal.id] = terminal
        self.terminal_id = terminal.id
        session_manager.update(
            self.recipient, status="paused", terminal_context={"gobby_terminal_id": terminal.id}
        )

    def dispatcher(self, sender: RecordingSender) -> WakeDispatcher:
        return WakeDispatcher(
            session_manager=self.session_manager,
            ism_manager=self.messages,
            tmux_sender=sender,
            terminal_manager=self.terminals,
        )

    async def send(self, dispatcher: WakeDispatcher, content: str) -> dict[str, Any]:
        result = await MailboxService(
            db=self.db,
            message_manager=self.messages,
            session_manager=self.session_manager,
            wake_dispatcher=dispatcher,
        ).send(
            from_session_id=self.sender_session.id,
            target="session",
            target_id=self.recipient,
            content=content,
            wake=True,
        )
        assert result.success is True
        [wake] = result.wake_results
        return wake

    def read_mailbox(self) -> None:
        pending = self.messages.get_undelivered_messages(self.recipient)
        self.messages.mark_delivered_batch([m.id for m in pending], self.recipient)


@pytest.fixture
def harness(
    temp_db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
) -> Harness:
    return Harness(temp_db, session_manager, sample_project["id"])


@pytest.mark.asyncio
async def test_messages_before_read_send_one_wake(harness: Harness) -> None:
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)

    first = await harness.send(dispatcher, "one")
    second = await harness.send(dispatcher, "two")
    third = await harness.send(dispatcher, "three")

    assert first["delivered"] is True
    assert second["skipped"] == "debounced"
    assert third["skipped"] == "debounced"
    assert sender.submitted == [harness.terminal_id]
    assert len(harness.messages.get_undelivered_messages(harness.recipient)) == 3


@pytest.mark.asyncio
async def test_reading_the_mailbox_allows_the_next_wake(harness: Harness) -> None:
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)

    await harness.send(dispatcher, "before read")
    harness.read_mailbox()
    after_read = await harness.send(dispatcher, "after read")
    held = await harness.send(dispatcher, "still unread")

    assert after_read["delivered"] is True
    assert held["skipped"] == "debounced"
    assert sender.submitted == [harness.terminal_id, harness.terminal_id]


@pytest.mark.asyncio
async def test_concurrent_deliveries_send_one_wake(harness: Harness) -> None:
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)

    results = await asyncio.gather(
        harness.send(dispatcher, "a"),
        harness.send(dispatcher, "b"),
        harness.send(dispatcher, "c"),
    )

    assert sorted(bool(result["delivered"]) for result in results) == [False, False, True]
    assert sender.submitted == [harness.terminal_id]


@pytest.mark.asyncio
async def test_failed_wake_does_not_suppress_the_next(harness: Harness) -> None:
    sender = RecordingSender(fail_first=True)
    dispatcher = harness.dispatcher(sender)

    failed = await harness.send(dispatcher, "lost")
    retried = await harness.send(dispatcher, "retry")

    assert failed["delivered"] is False
    assert retried["delivered"] is True
    assert sender.submitted == [harness.terminal_id]


@pytest.mark.asyncio
async def test_pending_wake_survives_a_dispatcher_restart(harness: Harness) -> None:
    sender = RecordingSender()
    await harness.send(harness.dispatcher(sender), "before restart")

    restarted = harness.dispatcher(sender)
    held = await harness.send(restarted, "after restart")
    harness.read_mailbox()
    woken = await harness.send(restarted, "after read")

    assert held["skipped"] == "debounced"
    assert woken["delivered"] is True
    assert sender.submitted == [harness.terminal_id, harness.terminal_id]


@pytest.mark.asyncio
async def test_partial_read_of_a_deferred_backlog_consumes_the_wake(harness: Harness) -> None:
    """A rendering budget may defer older rows; reading any one consumes the wake."""
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)
    for content in ("one", "two", "three"):
        await harness.send(dispatcher, content)
    first, *_deferred = harness.messages.get_undelivered_messages(harness.recipient)
    harness.messages.mark_delivered(first.id, harness.recipient)

    woken = await harness.send(dispatcher, "after partial read")

    assert woken["delivered"] is True
    assert sender.submitted == [harness.terminal_id, harness.terminal_id]


@pytest.mark.asyncio
async def test_lost_wake_expires_and_the_next_message_wakes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake reported delivered but never read stops suppressing once stale."""
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)
    await harness.send(dispatcher, "lost")
    later = utc_now() + timedelta(seconds=LIVE_WAKE_FRESH_SECONDS)
    monkeypatch.setattr("gobby.events.wake.utc_now", lambda: later)

    woken = await harness.send(dispatcher, "after expiry")

    assert woken["delivered"] is True
    assert sender.submitted == [harness.terminal_id, harness.terminal_id]


@pytest.mark.asyncio
async def test_urgent_wake_is_suppressed_while_a_fresh_wake_is_outstanding(
    harness: Harness,
) -> None:
    sender = RecordingSender()
    dispatcher = harness.dispatcher(sender)
    await harness.send(dispatcher, "first")

    urgent = await dispatcher.dispatch_live_wake(harness.recipient, priority="urgent")

    assert urgent["skipped"] == "debounced"
    assert sender.submitted == [harness.terminal_id]
