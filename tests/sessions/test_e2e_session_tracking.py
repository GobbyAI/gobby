import json
import os
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.sessions.processor import SessionMessageProcessor
from gobby.storage.hub.protocol import HubDatabase
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


class MockWebSocketServer:
    def __init__(self) -> None:
        self.broadcasted_messages: list[dict[str, Any]] = []

    async def broadcast(self, message: dict[str, Any]) -> None:
        self.broadcasted_messages.append(message)


@pytest.fixture
async def env(tmp_path: Path, hub_db: HubDatabase) -> AsyncGenerator[dict[str, Any]]:
    db = hub_db
    # Mock WebSocket
    ws = MockWebSocketServer()

    processor = SessionMessageProcessor(db, poll_interval=0.1, websocket_server=ws)

    # Configure mock config
    mock_config = MagicMock()
    mock_config.workflow.timeout = 0.0
    mock_config.workflow.enabled = True
    # Also need daemon config
    mock_config.daemon_health_check_interval = 10.0
    # Memory config must be None (not MagicMock) so default MemoryConfig is used
    mock_config.memory = None

    # Create HookManager
    hm = HookManager(
        daemon_host="test",
        message_processor=processor,
        config=mock_config,
        database=db,
    )

    # Force daemon status to be ready for tests
    with patch.object(
        hm,
        "_get_cached_daemon_status",
        MagicMock(return_value=(True, "OK", "running", None)),
    ):
        # Insert a valid project for FK constraints
        db.execute(
            "INSERT INTO projects (id, name, repo_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("proj-1", "Test Project", str(tmp_path), datetime.now(), datetime.now()),
        )

        # Start processor
        await processor.start()

        yield {"hm": hm, "proc": processor, "ws": ws, "db": db, "tmp": tmp_path}

        await processor.stop()
        hm.shutdown()


@pytest.mark.e2e
@pytest.mark.skipif(
    not os.environ.get("GOBBY_RUN_E2E_SESSION_LIFECYCLE"),
    reason="requires GOBBY_RUN_E2E_SESSION_LIFECYCLE until full-suite isolation is fixed",
)
async def test_full_lifecycle(env: dict[str, Any]) -> None:
    hm = env["hm"]
    ws = env["ws"]
    db = env["db"]
    tmp = env["tmp"]

    # 1. Prepare Transcript
    transcript_file = tmp / "session.jsonl"
    transcript_file.touch()

    # 2. Trigger SESSION_START
    start_event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="cli-session-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        data={"transcript_path": str(transcript_file), "cwd": str(tmp), "project_id": "proj-1"},
    )

    hm.handle(start_event)

    # Verify registration in DB
    row = db.fetchone("SELECT id FROM sessions WHERE external_id = ?", ("cli-session-1",))
    assert row is not None
    session_id = row["id"]

    # Verify registration in Processor
    assert session_id in env["proc"]._active_sessions

    # 3. Simulate Message Writing
    msg1 = json.dumps(
        {"type": "user", "message": {"content": "Hello E2E"}, "timestamp": "2024-01-01T12:00:00Z"}
    )

    with open(transcript_file, "w") as f:
        f.write(msg1 + "\n")

    await wait_for_async_condition(
        lambda: len(
            db.fetchall("SELECT * FROM session_messages WHERE session_id = ?", (session_id,))
        )
        >= 1,
        timeout=2.0,
        description="stored session message",
    )

    # 5. Verify DB Storage
    msgs = db.fetchall("SELECT * FROM session_messages WHERE session_id = ?", (session_id,))
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Hello E2E"

    # 6. Verify WebSocket Broadcast
    assert len(ws.broadcasted_messages) >= 1
    last_msg = ws.broadcasted_messages[-1]
    assert last_msg["type"] == "session_message"
    assert last_msg["session_id"] == session_id
    assert last_msg["message"]["content"] == "Hello E2E"

    # 7. Trigger SESSION_END
    end_event = HookEvent(
        event_type=HookEventType.SESSION_END,
        session_id="cli-session-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        metadata={"_platform_session_id": session_id},
        data={"transcript_path": str(transcript_file)},
    )

    hm.handle(end_event)

    # 8. Verify Unregistration (processor loop keeps running but shouldn't track session)
    assert session_id not in env["proc"]._active_sessions
