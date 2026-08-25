import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.token_usage import typed_json_token_usage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.token_events import TokenEvent, TokenEventStore
from tests.config_runtime_helpers import static_session_capture

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-4000-8000-000000000101"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-00000000001a"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_typed_json_token_usage_warns_when_cache_read_exceeds_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="gobby.sessions.token_usage"):
        usage = typed_json_token_usage(
            {
                "promptTokenCount": 10,
                "cachedContentTokenCount": 25,
                "candidatesTokenCount": 3,
            }
        )

    assert usage.input_tokens == 0
    assert usage.cache_read_tokens == 25
    assert any("exceeds promptTokenCount" in record.message for record in caplog.records)


@pytest.fixture
def db(tmp_path: Path, hub_db: HubDatabase) -> HubDatabase:
    """Initialize database with migrations."""
    database = hub_db
    # Create dummy project required for sessions
    database.execute(
        "INSERT INTO projects (id, name, repo_path) VALUES (%s, %s, %s)",
        (PROJECT_ID, "Test Project", str(tmp_path)),
    )
    return database


@pytest.fixture
def session_manager(db: HubDatabase) -> SessionManager:
    return SessionManager(db)


@pytest.fixture
def lifecycle_manager(db: HubDatabase) -> SessionLifecycleManager:
    config = SessionLifecycleConfig()
    return SessionLifecycleManager(db, static_session_capture(config))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_token_usage_aggregation(
    db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    tmp_path: Path,
) -> None:
    """Test that token usage is correctly aggregated from transcript files."""

    # 1. Create a dummy transcript with usage data
    # Format matches what ClaudeTranscriptParser expects
    transcript_path = tmp_path / "transcript.jsonl"

    transcript_data = [
        # Msg 1: Assistant msg with top-level usage
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
            "usage": {"input_tokens": 10, "output_tokens": 20, "cost": 0.001},
        },
        # Msg 2: User msg with top-level usage
        {
            "type": "user",
            "message": {"role": "user", "content": "Hi"},
            "usage": {"input_tokens": 5, "output_tokens": 0},
        },
        # Msg 3: Assistant msg with nested usage (Claude API style)
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Bye"}],
                "usage": {"input_tokens": 15, "output_tokens": 25, "cost": 0.002},
            },
        },
    ]

    with open(transcript_path, "w") as f:
        for entry in transcript_data:
            f.write(json.dumps(entry) + "\n")

    # 2. Register a session
    session = session_manager.register(
        external_id="ext-123",
        machine_id="21000000-0000-4000-8000-00000000001a",
        source="claude_code",
        project_id=PROJECT_ID,
        title="Test Session",
        transcript_path=str(transcript_path),
    )

    # 3. Process the transcript
    # We call the internal method directly to bypass status checks for testing
    await lifecycle_manager._process_session_transcript(session.id, str(transcript_path))

    # 4. Verify results
    updated_session = session_manager.get(session.id)

    assert updated_session is not None

    # Expected:
    # 1: 10 in, 20 out
    # 2: 5 in, 0 out
    # 3: 15 in, 25 out
    # Total: 30 in, 45 out

    assert updated_session.usage_input_tokens == 30
    assert updated_session.usage_output_tokens == 45


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batched_transcript_recording_matches_sequential_record_semantics(
    db: HubDatabase,
    session_manager: SessionManager,
    lifecycle_manager: SessionLifecycleManager,
    tmp_path: Path,
) -> None:
    """Differential guard for the batched off-loop insert path (#20885).

    Replays a transcript containing a duplicated message and a message already
    recorded by the live path, and asserts the batched pass stores exactly what
    sequential per-event ``record`` calls store — same per-event inserted
    flags, same rows, same running totals.
    """
    transcript_path = tmp_path / "transcript.jsonl"
    entries = [
        # Unique usage-bearing message: inserts.
        {
            "type": "assistant",
            "message": {
                "id": "msg-a",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        },
        # No usage: contributes no token event.
        {"type": "user", "message": {"role": "user", "content": "Hi"}},
        # Already recorded by the live path below: deduped against the table.
        {
            "type": "assistant",
            "message": {
                "id": "msg-live",
                "role": "assistant",
                "content": [{"type": "text", "text": "More"}],
                "usage": {"input_tokens": 5, "output_tokens": 7},
            },
        },
        # Duplicate of msg-a within the same transcript: deduped in-batch.
        {
            "type": "assistant",
            "message": {
                "id": "msg-a",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        },
    ]
    with open(transcript_path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    session = session_manager.register(
        external_id="ext-batch",
        machine_id=LOCAL_MACHINE_ID,
        source="claude_code",
        project_id=PROJECT_ID,
        title="Batched Session",
        transcript_path=str(transcript_path),
    )
    reference = session_manager.register(
        external_id="ext-sequential",
        machine_id=LOCAL_MACHINE_ID,
        source="claude_code",
        project_id=PROJECT_ID,
        title="Sequential Reference",
    )

    store = lifecycle_manager.token_event_store

    def _live_event(session_id: str) -> TokenEvent:
        return TokenEvent(
            session_id=session_id,
            project_id=PROJECT_ID,
            message_id="msg-live",
            source="claude",
            origin="live",
            model=None,
            input_tokens=100,
            output_tokens=200,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            event_at=datetime(2026, 8, 24, 11, 0, tzinfo=UTC),
        )

    assert store.record(_live_event(session.id)) is True
    assert store.record(_live_event(reference.id)) is True

    batched_events: list[TokenEvent] = []
    batched_flags: list[bool] = []
    original_record_batch = store.record_batch

    def _spy_record_batch(events: list[TokenEvent]) -> list[bool]:
        batched_events.extend(events)
        flags = original_record_batch(events)
        batched_flags.extend(flags)
        return flags

    with patch.object(store, "record_batch", side_effect=_spy_record_batch):
        await lifecycle_manager._process_session_transcript(session.id, str(transcript_path))

    # The fixture must actually exercise dedup: msg-a twice, msg-live once.
    usage_ids = [event.message_id for event in batched_events]
    assert usage_ids.count("msg-a") == 2
    assert usage_ids.count("msg-live") == 1

    # Sequential per-event record() over the same event stream is the
    # previous behavior; the batch must report identical inserted flags.
    reference_store = TokenEventStore(db)
    sequential_flags = [
        reference_store.record(replace(event, session_id=reference.id)) for event in batched_events
    ]
    assert batched_flags == sequential_flags

    # Same rows stored either way: the live row plus one row per unique
    # transcript message, with identical token counts.
    def _rows(session_id: str) -> list[tuple[str | None, str, int, int]]:
        return sorted(
            (e["message_id"], e["origin"], e["input_tokens"], e["output_tokens"])
            for e in reference_store.list_session_events(session_id)
        )

    assert _rows(session.id) == _rows(reference.id)
    assert reference_store.get_session_totals(session.id) == {
        "input_tokens": 110,
        "output_tokens": 220,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }
    assert reference_store.get_session_totals(reference.id) == reference_store.get_session_totals(
        session.id
    )

    # Session running totals aggregate live + unique transcript events only.
    updated_session = session_manager.get(session.id)
    assert updated_session is not None
    assert updated_session.usage_input_tokens == 110
    assert updated_session.usage_output_tokens == 220
