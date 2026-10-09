"""Expired transcript replay must not recreate retained token events."""

from __future__ import annotations

import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.token_events import TokenEvent, TokenEventStore
from gobby.utils.machine_id import require_machine_id
from tests.config_runtime_helpers import static_session_capture

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=180)


@pytest.fixture(autouse=True)
def retention_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gobby.storage.token_events.token_event_retention_cutoff", lambda: CUTOFF)
    monkeypatch.setattr(
        "gobby.sessions.transcript_processing.token_event_retention_cutoff", lambda: CUTOFF
    )


@pytest.mark.parametrize("message_id", ["expired-message", None])
def test_expired_replays_are_rejected_single_and_batch(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    message_id: str | None,
) -> None:
    session = session_manager.register(
        external_id="retention-replay",
        machine_id=require_machine_id(),
        source="claude",
        project_id=str(sample_project["id"]),
    )
    expired = TokenEvent(
        session_id=session.id,
        project_id=session.project_id,
        message_id=message_id,
        source="claude",
        origin="transcript",
        model=None,
        input_tokens=100,
        output_tokens=20,
        cache_creation_tokens=3,
        cache_read_tokens=4,
        event_at=NOW - timedelta(days=181),
    )
    store = TokenEventStore(temp_db)

    assert store.record(expired) is False
    assert store.record_batch([expired, expired]) == [False, False]
    assert store.count_session_events(session.id) == 0


@pytest.mark.parametrize(
    "age,accepted",
    [
        (timedelta(days=179), True),
        (timedelta(days=180) - timedelta(seconds=1), True),
        (timedelta(days=180), True),
        (timedelta(days=180) + timedelta(seconds=1), False),
        (timedelta(days=181), False),
    ],
)
def test_retention_boundary_and_batch_flags(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    age: timedelta,
    accepted: bool,
) -> None:
    session = session_manager.register(
        external_id="retention-boundary",
        machine_id=require_machine_id(),
        source="claude",
        project_id=str(sample_project["id"]),
    )
    event = TokenEvent(
        session_id=session.id,
        project_id=session.project_id,
        message_id="single",
        source="claude",
        origin="transcript",
        model=None,
        input_tokens=1,
        output_tokens=2,
        cache_creation_tokens=3,
        cache_read_tokens=4,
        event_at=NOW - age,
    )
    store = TokenEventStore(temp_db)
    assert store.record(event) is accepted
    expired = replace(event, message_id="expired", event_at=NOW - timedelta(days=181))
    current = replace(event, message_id=None, event_at=NOW)
    batch = replace(event, message_id="batch")
    assert store.record_batch([expired, batch, current, batch]) == [False, accepted, True, False]
    assert store.count_session_events(session.id) == 1 + 2 * int(accepted)


def test_cutoff_checks_the_timestamp_actually_stored(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = session_manager.register(
        external_id="retention-rounded-boundary",
        machine_id=require_machine_id(),
        source="claude",
        project_id=str(sample_project["id"]),
    )
    monkeypatch.setattr(
        "gobby.storage.token_events.token_event_retention_cutoff",
        lambda: CUTOFF + timedelta(microseconds=500_000),
    )
    event = TokenEvent(
        session_id=session.id,
        project_id=session.project_id,
        message_id="fractional",
        source="claude",
        origin="transcript",
        model=None,
        input_tokens=1,
        output_tokens=2,
        cache_creation_tokens=3,
        cache_read_tokens=4,
        event_at=CUTOFF + timedelta(microseconds=750_000),
    )
    # Storage truncates to seconds, so this row's persisted event_at is expired.
    store = TokenEventStore(temp_db)
    assert store.record(event) is False
    assert store.record_batch([event]) == [False]
    assert store.count_session_events(session.id) == 0


@pytest.mark.asyncio
async def test_lifetime_totals_survive_prune_full_replay_and_new_usage(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = tmp_path / "retention.jsonl"
    session = session_manager.register(
        external_id="retention-lifetime",
        machine_id=require_machine_id(),
        source="claude",
        project_id=str(sample_project["id"]),
        transcript_path=str(transcript),
    )
    store = TokenEventStore(temp_db)
    expired = TokenEvent(
        session_id=session.id,
        project_id=session.project_id,
        message_id="old",
        source="claude",
        origin="transcript",
        model=None,
        input_tokens=100,
        output_tokens=20,
        cache_creation_tokens=3,
        cache_read_tokens=4,
        event_at=NOW - timedelta(days=181),
    )
    # Simulate a row recorded before its retention deadline elapsed.
    with monkeypatch.context() as recording_clock:
        recording_clock.setattr(
            "gobby.storage.token_events.token_event_retention_cutoff",
            lambda: CUTOFF - timedelta(days=2),
        )
        assert store.record(expired)
    recent = replace(expired, message_id="recent", input_tokens=7, event_at=CUTOFF)
    assert store.record(recent)
    before = store.get_session_totals(session.id)
    assert before == {
        "input_tokens": 107,
        "output_tokens": 40,
        "cache_creation_tokens": 6,
        "cache_read_tokens": 8,
    }
    # Replay must preserve expired raw rows until the Rust worker archives them.
    assert store.delete_session_events(session.id, origin="transcript") == 1
    assert store.count_session_events(session.id) == 1
    assert store.record(recent)
    prune_sql = (Path(__file__).parents[2] / "crates/gdaemon/src/retention/prune.sql").read_text()
    deleted = temp_db.fetchone(prune_sql.replace("$1", "%s"), (CUTOFF.isoformat(),))
    assert deleted is not None and deleted["count"] == 1
    assert store.get_session_totals(session.id) == before
    assert store.count_session_events(session.id) == 1

    def write_transcript(events: list[TokenEvent]) -> None:
        entries = [
            {
                "type": "assistant",
                "timestamp": event.event_at.isoformat(),
                "message": {
                    "id": event.message_id,
                    "role": "assistant",
                    "model": "claude-sonnet-4",
                    "content": [{"type": "text", "text": "usage"}],
                    "usage": {
                        "input_tokens": event.input_tokens,
                        "output_tokens": event.output_tokens,
                        "cache_creation_input_tokens": event.cache_creation_tokens,
                        "cache_read_input_tokens": event.cache_read_tokens,
                    },
                },
            }
            for event in events
        ]
        transcript.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    lifecycle = SessionLifecycleManager(temp_db, static_session_capture(SessionLifecycleConfig()))
    # The cutoff advances during replay. Its shared snapshot must prevent deleting
    # a boundary event and then rejecting its replacement as newly expired.
    monkeypatch.setattr(
        "gobby.storage.token_events.token_event_retention_cutoff",
        lambda: CUTOFF + timedelta(seconds=1),
    )

    def retention_can_own_cycle() -> bool:
        # Another thread has no ambient replay transaction: it is a second hub connection.
        with temp_db.transaction():
            row = temp_db.fetchone(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS acquired",
                ("hub-retention:token-events",),
            )
            assert row is not None
            return bool(row["acquired"])

    record_batch = store.record_batch

    def insert_while_retention_attempts(
        events: Sequence[TokenEvent], *, retention_cutoff: datetime | None = None
    ) -> list[bool]:
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(retention_can_own_cycle).result(timeout=5) is False
        return record_batch(events, retention_cutoff=retention_cutoff)

    monkeypatch.setattr(
        lifecycle.token_event_store, "record_batch", insert_while_retention_attempts
    )
    write_transcript([expired, recent])
    for _ in range(2):
        await lifecycle._process_session_transcript(session.id, str(transcript))
        assert store.get_session_totals(session.id) == before
        assert store.count_session_events(session.id) == 1
        updated = session_manager.get(session.id)
        assert updated is not None and updated.usage_input_tokens == 107
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(retention_can_own_cycle).result(timeout=5) is True
    new = replace(
        recent,
        message_id="new",
        input_tokens=11,
        output_tokens=5,
        cache_creation_tokens=2,
        cache_read_tokens=1,
        event_at=NOW,
    )
    write_transcript([expired, recent, new])
    await lifecycle._process_session_transcript(session.id, str(transcript))
    assert store.get_session_totals(session.id) == {
        "input_tokens": 118,
        "output_tokens": 45,
        "cache_creation_tokens": 8,
        "cache_read_tokens": 9,
    }
    assert store.count_session_events(session.id) == 2
    updated = session_manager.get(session.id)
    assert updated is not None and updated.usage_input_tokens == 118
