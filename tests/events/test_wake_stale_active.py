"""A bound Codex seat stranded active after its turn ended still takes a wake (#23102)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.agents.idle_detector import ComposerRead, ComposerState
from gobby.events.live_wake import TerminalActivity
from gobby.events.wake import WakeDispatcher
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.integration

_STRANDED_AT = datetime(2026, 9, 28, 14, 30, tzinfo=UTC)


async def _run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


async def _flush(_session_id: str) -> None:
    return None


def _rollout(path: Path, *turn_events: str) -> Path:
    records = [{"type": "session_meta", "payload": {"id": "rollout"}}]
    records += [{"type": "event_msg", "payload": {"type": event}} for event in turn_events]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def _stranded_codex_seat(
    session_manager: SessionManager,
    project_id: str,
    transcript: Path,
) -> str:
    """Register a Codex row and leave it active well after the restart horizon."""
    session_id = session_manager.register(
        external_id="stranded-codex",
        machine_id=None,
        source="codex",
        project_id=project_id,
        title="stranded-codex",
        transcript_path=str(transcript),
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
    transcript: Path,
    reads: list[TerminalActivity],
    *,
    after_first_read: Any = None,
) -> tuple[str, AsyncMock, dict[str, Any]]:
    """Queue one mailbox wake and run the real decline, reconcile, CAS and retry."""
    recipient_id = _stranded_codex_seat(session_manager, project_id, transcript)
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
    probed = 0

    async def probe(_session: object, _terminal: object | None) -> TerminalActivity:
        nonlocal probed
        probed += 1
        if probed == 1 and after_first_read is not None:
            after_first_read(recipient_id)
        return reads[min(probed, len(reads)) - 1]

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
    return recipient_id, sender, first


def _deferred_line(caplog: pytest.LogCaptureFixture, session_id: str) -> str:
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(f"Deferred wake for session {session_id}:")
    ]
    assert len(lines) == 1
    return lines[0]


@pytest.mark.parametrize("composer", ["empty", "unknown"])
async def test_settled_rollout_pauses_stranded_row_and_delivers_wake(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    composer: ComposerState,
) -> None:
    """Settled rollout, stale active row, no in-flight fingerprint: pause, then wake."""
    transcript = _rollout(tmp_path / "rollout.jsonl", "task_started", "task_complete")
    caplog.set_level(logging.INFO, logger="gobby.events.wake")

    session_id, sender, _ = await _deferred_wake(
        temp_db,
        session_manager,
        sample_project["id"],
        transcript,
        [TerminalActivity(ComposerRead(composer))],
    )

    row = session_manager.get(session_id)
    assert row is not None and row.status == "paused"
    sender.assert_awaited_once()
    line = _deferred_line(caplog, session_id)
    assert "delivered=True" in line
    assert "idle=paused" in line
    assert "reconcile_ms=" in line


@pytest.mark.parametrize(
    ("turn_events", "activity", "reason"),
    [
        pytest.param(
            ("task_complete", "task_started"),
            TerminalActivity(ComposerRead("unknown")),
            "composer_unknown",
            id="rollout-turn-open",
        ),
        pytest.param(
            (),
            TerminalActivity(ComposerRead("unknown")),
            "composer_unknown",
            id="rollout-without-turn",
        ),
        pytest.param(
            ("task_complete",),
            TerminalActivity(ComposerRead("empty"), "spinner:esc to interrupt"),
            "turn_in_flight",
            id="live-spinner",
        ),
        pytest.param(
            ("task_complete",),
            TerminalActivity(ComposerRead("draft", "half-typed reply")),
            "draft",
            id="draft",
        ),
    ],
)
async def test_active_turn_protection_keeps_row_active(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    turn_events: tuple[str, ...],
    activity: TerminalActivity,
    reason: str,
) -> None:
    """An open turn, a live fingerprint, or a draft never loses its active status."""
    transcript = _rollout(tmp_path / "rollout.jsonl", *turn_events)
    caplog.set_level(logging.INFO, logger="gobby.events.wake")

    session_id, sender, _ = await _deferred_wake(
        temp_db,
        session_manager,
        sample_project["id"],
        transcript,
        [activity],
    )

    row = session_manager.get(session_id)
    assert row is not None and row.status == "active"
    assert row.updated_at == _STRANDED_AT
    sender.assert_not_awaited()
    line = _deferred_line(caplog, session_id)
    assert "skipped=session_active" in line
    assert f"idle={reason}" in line


async def test_row_touched_between_reads_is_refused_by_exact_cas(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lifecycle write that lands during the reads wins over the stale observation."""
    transcript = _rollout(tmp_path / "rollout.jsonl", "task_complete")
    caplog.set_level(logging.INFO, logger="gobby.events.wake")
    touched_at = _STRANDED_AT + timedelta(microseconds=1)

    def touch(session_id: str) -> None:
        with session_manager.db.transaction():
            session_manager.db.execute(
                "UPDATE sessions SET updated_at = %s WHERE id = %s",
                (touched_at, session_id),
            )

    session_id, sender, _ = await _deferred_wake(
        temp_db,
        session_manager,
        sample_project["id"],
        transcript,
        [TerminalActivity(ComposerRead("unknown"))],
        after_first_read=touch,
    )

    row = session_manager.get(session_id)
    assert row is not None and row.status == "active"
    assert row.updated_at == touched_at
    sender.assert_not_awaited()
    assert "idle=row_changed" in _deferred_line(caplog, session_id)
