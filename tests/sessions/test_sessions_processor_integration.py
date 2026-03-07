import asyncio
import json
from collections.abc import AsyncGenerator

import pytest

from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.database import LocalDatabase

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_db(tmp_path) -> LocalDatabase:
    # Use file-based DB for tests (in-memory doesn't work with asyncio.to_thread
    # because each thread gets a separate connection/database)
    return LocalDatabase(tmp_path / "test.db")


@pytest.fixture
async def processor(mock_db: LocalDatabase) -> AsyncGenerator[SessionMessageProcessor]:
    proc = SessionMessageProcessor(mock_db, poll_interval=0.1)
    # Ensure tables exist
    # Note: In real app, migrations run; here we must ensure schema
    # But for now, assuming LocalDatabase fixture or setup might handle it
    # If not, we might need to apply schema manually.
    # Let's verify if LocalMessageManager requires tables created.
    # We'll apply the schema manually for the test to be safe.

    # Create sessions table (required for foreign key constraint)
    mock_db.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            external_id TEXT NOT NULL,
            machine_id TEXT NOT NULL,
            source TEXT NOT NULL,
            project_id TEXT,
            title TEXT,
            status TEXT DEFAULT 'active',
            jsonl_path TEXT,
            summary_path TEXT,
            summary_markdown TEXT,
            git_branch TEXT,
            parent_session_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """
    )
    mock_db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            message_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            content_type TEXT DEFAULT 'text',
            tool_name TEXT,
            tool_input TEXT,
            tool_result TEXT,
            tool_use_id TEXT,
            timestamp TEXT NOT NULL,
            raw_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(session_id, message_index)
        );
    """
    )
    mock_db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_transcripts (
            session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
            transcript_blob BLOB NOT NULL,
            uncompressed_size INTEGER NOT NULL,
            compressed_size INTEGER NOT NULL,
            checksum TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """
    )
    mock_db.execute(
        """
        CREATE TABLE IF NOT EXISTS session_message_state (
            session_id TEXT PRIMARY KEY,
            last_byte_offset INTEGER DEFAULT 0,
            last_message_index INTEGER DEFAULT 0,
            last_processed_at TEXT,
            processing_errors INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """
    )

    # Insert test sessions (required for message storage)
    for session_id in ["session-1", "s1", "s2"]:
        mock_db.execute(
            """
            INSERT OR IGNORE INTO sessions (id, external_id, machine_id, source)
            VALUES (?, ?, 'test-machine', 'test')
            """,
            (session_id, session_id),
        )

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
async def test_processor_lifecycle(processor, transcript_file, mock_db):
    # 1. Start processor
    await processor.start()
    assert processor._running
    assert processor._task is not None

    # 2. Register session
    processor.register_session("session-1", str(transcript_file))

    # 3. Write lines
    msg1 = json.dumps(
        {"type": "user", "message": {"content": "Hello"}, "timestamp": "2024-01-01T10:00:00Z"}
    )

    with open(transcript_file, "w") as f:
        f.write(msg1 + "\n")

    # 4. Wait for poll (interval is 0.1s in fixture)
    await asyncio.sleep(0.3)

    # 5. Verify DB
    rows = mock_db.fetchall("SELECT * FROM session_messages WHERE session_id = ?", ("session-1",))
    assert len(rows) == 1
    assert rows[0]["content"] == "Hello"

    # 6. Stop
    await processor.stop()
    assert not processor._running


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_incremental_processing(processor, transcript_file, mock_db):
    await processor.start()
    processor.register_session("session-1", str(transcript_file))

    # Initial write
    msg1 = json.dumps(
        {"type": "user", "message": {"content": "msg1"}, "timestamp": "2024-01-01T10:00:00Z"}
    )
    with open(transcript_file, "w") as f:
        f.write(msg1 + "\n")

    await asyncio.sleep(0.2)

    # Verify first msg
    rows = mock_db.fetchall("SELECT * FROM session_messages")
    assert len(rows) == 1
    assert rows[0]["content"] == "msg1"

    # Verify state
    state = mock_db.fetchone(
        "SELECT * FROM session_message_state WHERE session_id = ?", ("session-1",)
    )
    assert state["last_byte_offset"] > 0
    assert state["last_message_index"] == 0

    # Append new msg
    msg2 = json.dumps(
        {"type": "agent", "message": {"content": "msg2"}, "timestamp": "2024-01-01T10:01:00Z"}
    )
    with open(transcript_file, "a") as f:
        f.write(msg2 + "\n")

    await asyncio.sleep(0.2)

    # Verify total msgs
    rows = mock_db.fetchall("SELECT * FROM session_messages ORDER BY message_index")
    assert len(rows) == 2
    assert rows[1]["content"] == "msg2"

    # Ensure no duplicates (unique constraint would fail or count would be wrong if naive)
    count = mock_db.fetchone("SELECT COUNT(*) as c FROM session_messages")["c"]
    assert count == 2


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_recovery_after_restart(processor, transcript_file, mock_db):
    # Pre-seed file with 2 messages
    msgs = [
        json.dumps(
            {"type": "user", "message": {"content": "msg1"}, "timestamp": "2024-01-01T10:00:00Z"}
        ),
        json.dumps(
            {"type": "agent", "message": {"content": "msg2"}, "timestamp": "2024-01-01T10:01:00Z"}
        ),
    ]
    with open(transcript_file, "w") as f:
        f.write(msgs[0] + "\n")
        offset_after_first = f.tell()
        f.write(msgs[1] + "\n")

    # Pre-seed DB state saying we processed msg1 (offset pointing to start of msg2)
    mock_db.execute(
        """
        INSERT INTO session_message_state (session_id, last_byte_offset, last_message_index)
        VALUES (?, ?, ?)
    """,
        ("session-1", offset_after_first, 0),
    )

    # Pre-seed msg1 in message table so we don't violate unique constraint if we tried to re-insert
    # But wait, we want to prove it DOESN'T try to re-process msg1.
    # If it ignored offset, it would read from 0, parse msg1, and try to insert.
    # The INSERT ON CONFLICT UPDATE in LocalMessageManager handles duplicates gracefully,
    # so we wouldn't get an error.
    # To prove it skipped, we can check logs or side effects, OR we can verify it processed msg2 quickly.

    await processor.start()
    processor.register_session("session-1", str(transcript_file))

    await asyncio.sleep(0.2)

    # Result: Should have msg2 in DB. msg1 should NOT be re-inserted (it's not in DB, so if it read it, it would insert it)
    # If it respected offset, it skipped msg1. So msg1 should be MISSING from DB if we didn't pre-insert it.

    rows = mock_db.fetchall("SELECT * FROM session_messages WHERE session_id = ?", ("session-1",))

    # We expect ONLY msg2 if it skipped msg1 (since we didn't pre-seed msg1 in messages table)
    # The file has msg1 and msg2.
    # State says "we read up to end of msg1".
    # So processor should seek to offset_after_first, read msg2, and insert it.
    # DB will contain ONLY msg2.

    assert len(rows) == 1
    assert rows[0]["content"] == "msg2"
    assert rows[0]["message_index"] == 1


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.slow
async def test_concurrent_sessions(processor, tmp_path, mock_db):
    # Create two transcript files
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

    await asyncio.sleep(0.3)

    # Verify both processed
    rows1 = mock_db.fetchall("SELECT * FROM session_messages WHERE session_id='s1'")
    rows2 = mock_db.fetchall("SELECT * FROM session_messages WHERE session_id='s2'")

    assert len(rows1) == 1
    assert rows1[0]["content"] == "s1_msg"

    assert len(rows2) == 1
    assert rows2[0]["content"] == "s2_msg"
