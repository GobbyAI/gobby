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
from gobby.sessions.transcript_reader import TranscriptReader
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests._timing import wait_for_async_condition
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit


class MockWebSocketServer:
    def __init__(self) -> None:
        self.broadcasted_messages: list[dict[str, Any]] = []
        self.tts_events: list[tuple[str, dict[str, Any], bool]] = []

    async def broadcast(self, message: dict[str, Any]) -> None:
        self.broadcasted_messages.append(message)

    async def broadcast_session_usage_updated(self, message: dict[str, Any]) -> None:
        await self.broadcast(message)

    async def broadcast_token_event(self, message: dict[str, Any]) -> None:
        await self.broadcast(message)

    # TODO: Exercise MockWebSocketServer.feed_attached_session_tts in future TTS E2E coverage.
    async def feed_attached_session_tts(
        self,
        session_id: str,
        rendered: dict[str, Any],
        *,
        complete: bool = False,
    ) -> None:
        self.tts_events.append((session_id, rendered, complete))


@pytest.fixture
async def env(
    tmp_path: Path,
    hub_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[dict[str, Any]]:
    db = hub_db
    home = tmp_path / "gobby-home"
    files_home = home / "files"
    files_home.mkdir(parents=True)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(f"datastore_mode: local\nfiles_home: {files_home}\n", encoding="utf-8")
    bootstrap.chmod(0o600)
    monkeypatch.setenv("GOBBY_HOME", str(home))
    project = isolated_checkout_factory(db, "Test Project", root=tmp_path)
    # Mock WebSocket
    ws = MockWebSocketServer()

    session_manager = SessionManager(db)
    processor = SessionMessageProcessor(
        db, poll_interval=0.1, websocket_server=ws, session_manager=session_manager
    )

    # Configure mock config
    mock_config = MagicMock()
    mock_config.workflow.timeout = 0.0
    mock_config.workflow.enabled = True
    # Also need daemon config
    mock_config.daemon_health_check_interval = 10.0
    # Memory config must be None (not MagicMock) so default MemoryConfig is used
    mock_config.memory = None

    # Keep daemon health checks isolated while exercising real hook/session handling.
    with patch("gobby.hooks.factory.HealthMonitor", autospec=True) as monitor_type:
        monitor_type.return_value.get_cached_status.return_value = (True, "OK", "running", None)
        hm = HookManager(
            daemon_host="test",
            message_processor_resolver=lambda: processor,
            config=mock_config,
            database=db,
            session_manager=session_manager,
        )
        # Start processor
        await processor.start()
        try:
            yield {
                "hm": hm,
                "proc": processor,
                "ws": ws,
                "db": db,
                "tmp": tmp_path,
                "project_id": project.project.id,
                "machine_id": project.machine_id,
                "reader": TranscriptReader(session_manager),
            }
        finally:
            await processor.stop()
            await hm.shutdown_async()


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
        machine_id=env["machine_id"],
        data={
            "transcript_path": str(transcript_file),
            "cwd": str(tmp),
            "project_id": env["project_id"],
        },
    )

    response = await hm.handle_async(start_event)
    assert response.decision == "allow"
    # Ordinary startup is stateless; first activity materializes the session.
    prompt_event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="cli-session-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        machine_id=env["machine_id"],
        data={**start_event.data, "prompt": "Hello E2E"},
    )
    await hm.handle_async(prompt_event)

    # Verify registration in DB
    row = db.fetchone("SELECT id FROM sessions WHERE external_id = %s", ("cli-session-1",))
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
        lambda: any(message["type"] == "session_message" for message in ws.broadcasted_messages),
        timeout=2.0,
        description="processed session message",
    )

    # Verify transcript-backed message storage through the current reader API.
    msgs = await env["reader"].get_messages(session_id)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "Hello E2E"

    # 6. Verify WebSocket Broadcast
    assert len(ws.broadcasted_messages) >= 1
    last_msg = [m for m in ws.broadcasted_messages if m["type"] == "session_message"][-1]
    assert last_msg["type"] == "session_message"
    assert last_msg["session_id"] == session_id
    assert last_msg["message"]["content"] == "Hello E2E"

    # 7. Trigger SESSION_END
    end_event = HookEvent(
        event_type=HookEventType.SESSION_END,
        session_id="cli-session-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        machine_id=env["machine_id"],
        metadata={"_platform_session_id": session_id},
        data={"transcript_path": str(transcript_file)},
    )

    await hm.handle_async(end_event)

    # 8. Verify Unregistration (processor loop keeps running but shouldn't track session)
    assert session_id not in env["proc"]._active_sessions
