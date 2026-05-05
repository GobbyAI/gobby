"""Integration tests for SessionMessageProcessor.

The processor reads JSONL transcript files, parses messages via TranscriptParser,
computes in-memory stats, and (optionally) writes stats to the sessions table
via session_manager.  It does NOT write to a session_messages table.
"""

import json
from collections.abc import AsyncGenerator

import pytest

from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.database import LocalDatabase
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_db(tmp_path) -> LocalDatabase:
    return LocalDatabase(tmp_path / "test.db")


@pytest.fixture
async def processor(mock_db: LocalDatabase) -> AsyncGenerator[SessionMessageProcessor]:
    proc = SessionMessageProcessor(mock_db, poll_interval=0.1)
    yield proc
    if proc._running:
        await proc.stop()


@pytest.fixture
def transcript_file(tmp_path):
    f = tmp_path / "transcript.jsonl"
    f.touch()
    return f


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_processor_lifecycle(processor, transcript_file):
    """Test that the processor starts, processes messages, and stops correctly."""
    # 1. Start processor
    await processor.start()
    assert processor._running
    assert processor._task is not None

    # 2. Register session
    processor.register_session("session-1", str(transcript_file))

    # 3. Write a transcript line
    msg1 = json.dumps(
        {"type": "user", "message": {"content": "Hello"}, "timestamp": "2024-01-01T10:00:00Z"}
    )
    with open(transcript_file, "w") as f:
        f.write(msg1 + "\n")

    await wait_for_async_condition(
        lambda: processor._stats.get("session-1", {}).get("message_count", 0) >= 1,
        description="initial transcript processing",
    )

    # 5. Verify in-memory stats were computed
    stats = processor._stats.get("session-1", {})
    assert stats.get("message_count", 0) >= 1

    # 6. Stop
    await processor.stop()
    assert not processor._running


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_incremental_processing(processor, transcript_file):
    """Test that the processor reads incrementally and doesn't re-process old messages."""
    await processor.start()
    processor.register_session("session-1", str(transcript_file))

    # Initial write
    msg1 = json.dumps(
        {"type": "user", "message": {"content": "msg1"}, "timestamp": "2024-01-01T10:00:00Z"}
    )
    with open(transcript_file, "w") as f:
        f.write(msg1 + "\n")

    await wait_for_async_condition(
        lambda: processor._stats.get("session-1", {}).get("message_count", 0) >= 1,
        description="first incremental transcript processing",
    )

    # Verify first message processed
    stats = processor._stats.get("session-1", {})
    assert stats.get("message_count", 0) >= 1

    # Verify byte offset advanced
    assert processor._byte_offsets.get("session-1", 0) > 0

    # Append new msg
    msg2 = json.dumps(
        {"type": "assistant", "message": {"content": "msg2"}, "timestamp": "2024-01-01T10:01:00Z"}
    )
    with open(transcript_file, "a") as f:
        f.write(msg2 + "\n")

    await wait_for_async_condition(
        lambda: processor._stats.get("session-1", {}).get("message_count", 0) >= 2,
        description="second incremental transcript processing",
    )

    # Verify total messages counted (stats accumulate)
    stats = processor._stats.get("session-1", {})
    assert stats.get("message_count", 0) >= 2


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_recovery_after_restart(processor, transcript_file):
    """Test that the processor resumes from the last byte offset after restart."""
    # Pre-seed file with 2 messages
    msgs = [
        json.dumps(
            {"type": "user", "message": {"content": "msg1"}, "timestamp": "2024-01-01T10:00:00Z"}
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": "msg2"},
                "timestamp": "2024-01-01T10:01:00Z",
            }
        ),
    ]
    with open(transcript_file, "w") as f:
        f.write(msgs[0] + "\n")
        offset_after_first = f.tell()
        f.write(msgs[1] + "\n")

    # Pre-seed in-memory offset to simulate previous processing of msg1
    processor._byte_offsets["session-1"] = offset_after_first
    processor._message_indices["session-1"] = 0

    await processor.start()
    processor.register_session("session-1", str(transcript_file))

    await wait_for_async_condition(
        lambda: processor._stats.get("session-1", {}).get("message_count", 0) == 1,
        description="restart transcript processing",
    )

    # Processor should have only processed msg2 (skipping msg1 via offset)
    stats = processor._stats.get("session-1", {})
    assert stats.get("message_count", 0) == 1  # Only msg2


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_concurrent_sessions(processor, tmp_path):
    """Test that the processor handles multiple sessions concurrently."""
    file1 = tmp_path / "t1.jsonl"
    file2 = tmp_path / "t2.jsonl"
    file1.touch()
    file2.touch()

    await processor.start()
    processor.register_session("s1", str(file1))
    processor.register_session("s2", str(file2))

    # Write to both
    with open(file1, "w") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": "s1_msg"},
                    "timestamp": "2024-01-01T10:00:00Z",
                }
            )
            + "\n"
        )

    with open(file2, "w") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": "s2_msg"},
                    "timestamp": "2024-01-01T10:00:00Z",
                }
            )
            + "\n"
        )

    await wait_for_async_condition(
        lambda: (
            processor._stats.get("s1", {}).get("message_count", 0) >= 1
            and processor._stats.get("s2", {}).get("message_count", 0) >= 1
        ),
        description="concurrent transcript processing",
    )

    # Verify both sessions processed
    stats_s1 = processor._stats.get("s1", {})
    stats_s2 = processor._stats.get("s2", {})

    assert stats_s1.get("message_count", 0) >= 1
    assert stats_s2.get("message_count", 0) >= 1
