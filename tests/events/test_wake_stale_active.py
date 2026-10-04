"""A bound Codex seat stranded active at an empty prompt still takes a wake (#23102)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake import WakeDispatcher
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.integration

_STRANDED_AT = datetime(2026, 9, 28, 14, 30, tzinfo=UTC)
_EMPTY = TerminalActivity(ComposerRead("empty"))


@dataclass
class _Wake:
    session_id: str
    sender: AsyncMock
    messages: InterSessionMessageManager
    dispatcher: WakeDispatcher


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


async def _flush(_session_id: str) -> None:
    return None


def _stranded_codex_seat(session_manager: SessionManager, project_id: str) -> str:
    """Register a Codex row and leave it active well after any restart horizon."""
    session_id = session_manager.register(
        external_id="stranded-codex",
        machine_id=None,
        source="codex",
        project_id=project_id,
        title="stranded-codex",
    ).id
    with session_manager.db.transaction():
        session_manager.db.execute(
            "UPDATE sessions SET status = 'active', updated_at = %s, last_activity = %s "
            "WHERE id = %s",
            (_STRANDED_AT, _STRANDED_AT, session_id),
        )
    return session_id


async def _deferred_wake(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    read: Callable[[str], TerminalActivity],
) -> _Wake:
    """Queue one mailbox wake and run the real decline, reconcile, CAS and retry."""
    recipient_id = _stranded_codex_seat(session_manager, project_id)
    sender_id = session_manager.register(
        external_id="wake-sender",
        machine_id=None,
        source="claude",
        project_id=project_id,
        title="wake-sender",
    ).id
    messages = InterSessionMessageManager(temp_db)
    messages.create_message(
        from_session=sender_id,
        to_session=recipient_id,
        content="pending wake",
        metadata_json='{"wake_requested": true}',
    )
    terminal = replace(make_memory_terminal(backend="native"), session_id=recipient_id)
    sender = AsyncMock(return_value=None)

    async def probe(_session: object, _terminal: object | None) -> TerminalActivity:
        return read(recipient_id)

    dispatcher = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=messages,
        tmux_sender=sender,
        terminal_manager=MemoryTerminalStore(terminal),
        run_db=_run_db,
        lifecycle_refresh=_flush,
        activity_probe=probe,
    )
    first = await dispatcher.dispatch_live_wake(recipient_id)
    assert first["skipped"] == "session_active"
    sender.assert_not_awaited()
    await asyncio.wait_for(dispatcher._deferred_refreshes[recipient_id], timeout=2)
    return _Wake(recipient_id, sender, messages, dispatcher)


def _deferred_line(caplog: pytest.LogCaptureFixture, session_id: str) -> str:
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(f"Deferred wake for session {session_id}:")
    ]
    assert len(lines) == 1
    return lines[0]


async def test_empty_prompt_pauses_stranded_row_and_delivers_wake(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stale active row, positive empty composer, no in-flight fingerprint: pause, then wake."""
    caplog.set_level(logging.INFO, logger="gobby.events.wake")

    wake = await _deferred_wake(temp_db, session_manager, sample_project["id"], lambda _id: _EMPTY)

    row = session_manager.get(wake.session_id)
    assert row is not None and row.status == "paused"
    wake.sender.assert_awaited_once()
    line = _deferred_line(caplog, wake.session_id)
    assert "delivered=True" in line
    assert "idle=paused" in line
    assert "reconcile_ms=" in line


def _probe_error(_session_id: str) -> TerminalActivity:
    raise RuntimeError("snapshot failed")


@pytest.mark.parametrize(
    ("read", "reason"),
    [
        pytest.param(
            lambda _id: TerminalActivity(ComposerRead("unknown")),
            "composer_unknown",
            id="unknown-composer",
        ),
        pytest.param(_probe_error, "composer_unknown", id="probe-error"),
        pytest.param(
            lambda _id: TerminalActivity(ComposerRead("empty"), "spinner:esc to interrupt"),
            "turn_in_flight",
            id="live-spinner",
        ),
        pytest.param(
            lambda _id: TerminalActivity(ComposerRead("draft", "half-typed reply")),
            "composer_draft",
            id="draft",
        ),
    ],
)
async def test_unconfirmed_prompt_keeps_row_active_and_message_durable(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    read: Callable[[str], TerminalActivity],
    reason: str,
) -> None:
    """Without a positive empty prompt the row stays active and no terminal input is sent."""
    caplog.set_level(logging.INFO, logger="gobby.events.wake")

    wake = await _deferred_wake(temp_db, session_manager, sample_project["id"], read)

    row = session_manager.get(wake.session_id)
    assert row is not None and row.status == "active"
    assert row.updated_at == _STRANDED_AT
    wake.sender.assert_not_awaited()
    assert [m.content for m in wake.messages.get_undelivered_wake_messages(wake.session_id)] == [
        "pending wake"
    ]
    line = _deferred_line(caplog, wake.session_id)
    assert "skipped=session_active" in line
    assert f"idle={reason}" in line


async def test_row_touched_between_reads_is_refused_by_exact_cas(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lifecycle write that lands during the reads wins over the stale observation."""
    caplog.set_level(logging.INFO, logger="gobby.events.wake")
    touched_at = _STRANDED_AT + timedelta(microseconds=1)
    reads = 0

    def touch_on_first_read(session_id: str) -> TerminalActivity:
        nonlocal reads
        reads += 1
        if reads == 1:
            with session_manager.db.transaction():
                session_manager.db.execute(
                    "UPDATE sessions SET updated_at = %s WHERE id = %s",
                    (touched_at, session_id),
                )
        return _EMPTY

    wake = await _deferred_wake(temp_db, session_manager, sample_project["id"], touch_on_first_read)

    row = session_manager.get(wake.session_id)
    assert row is not None and row.status == "active"
    assert row.updated_at == touched_at
    wake.sender.assert_not_awaited()
    assert "idle=row_changed" in _deferred_line(caplog, wake.session_id)


async def _cancel_followups(dispatcher: WakeDispatcher) -> None:
    """Cancel the bounded composer retry a withheld wake schedules."""
    for task in list(dispatcher._composer_retries.values()):
        task.cancel()
    for task in list(dispatcher._deferred_refreshes.values()):
        task.cancel()
    await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("send_read", "skipped"),
    [
        pytest.param(
            lambda: TerminalActivity(ComposerRead("unknown")),
            "composer_unconfirmed",
            id="unknown",
        ),
        pytest.param(
            lambda: TerminalActivity(ComposerRead("empty"), turn_in_flight_fingerprint="run-7"),
            "composer_unconfirmed",
            id="turn_in_flight",
        ),
        pytest.param(_probe_error, "composer_unconfirmed", id="probe_error"),
        pytest.param(
            lambda: TerminalActivity(ComposerRead("draft", "half-typed reply")),
            "composer_occupied",
            id="draft",
        ),
    ],
)
async def test_send_time_blocked_composer_withholds_and_keeps_message_durable(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    send_read: Callable[[], TerminalActivity],
    skipped: str,
) -> None:
    """#23102: after the reconcile pauses the row, a re-probe at the send boundary
    that is unconfirmed or finds an operator draft must not reach the terminal
    sender or drop the durable message.
    """
    caplog.set_level(logging.INFO, logger="gobby.events.wake")
    reads = 0

    def read_on_send_past_reconcile(_session_id: str) -> TerminalActivity:
        nonlocal reads
        reads += 1
        # Reads 1-2 reconcile the stale-active row; read 3 is the send re-probe.
        return _EMPTY if reads <= 2 else send_read()

    wake = await _deferred_wake(
        temp_db, session_manager, sample_project["id"], read_on_send_past_reconcile
    )

    row = session_manager.get(wake.session_id)
    assert row is not None and row.status == "paused"
    assert reads == 3, reads
    wake.sender.assert_not_awaited()
    assert [m.content for m in wake.messages.get_undelivered_wake_messages(wake.session_id)] == [
        "pending wake"
    ]
    line = _deferred_line(caplog, wake.session_id)
    assert f"skipped={skipped}" in line
    await _cancel_followups(wake.dispatcher)
