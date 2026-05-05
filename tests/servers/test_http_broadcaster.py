from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.extensions import HookExtensionsConfig, WebSocketBroadcastConfig
from gobby.hooks.broadcaster import HookEventBroadcaster
from gobby.hooks.events import HookEvent, HookEventType, SessionSource

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_broadcaster_broadcasts_session_start_event():
    """Test that HookEventBroadcaster correctly broadcasts session-start events."""
    # Setup config with broadcasting enabled
    config = DaemonConfig()
    config.hook_extensions = HookExtensionsConfig(
        websocket=WebSocketBroadcastConfig(
            enabled=True,
            broadcast_events=["session-start"],
            include_payload=True,
        )
    )

    # Setup mocked websocket server
    mock_ws = AsyncMock()

    # Create broadcaster directly (this is what we're actually testing)
    broadcaster = HookEventBroadcaster(websocket_server=mock_ws, config=config)

    # Create a session-start event
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        data={
            "project_path": "/tmp/test",
            "resume": False,
            "transcript_path": "/tmp/transcript.jsonl",
        },
    )

    # Directly call broadcast_event (no timing issues since we await it)
    await broadcaster.broadcast_event(event)

    # Verify websocket broadcast was called
    mock_ws.broadcast.assert_called_once()

    # Verify payload content
    call_args = mock_ws.broadcast.call_args
    broadcast_payload = call_args[0][0]

    assert broadcast_payload["type"] == "hook_event"
    assert broadcast_payload["event_type"] == "session-start"
    assert broadcast_payload.get("session_id") == "test-session"
    assert "data" in broadcast_payload


@pytest.mark.asyncio
async def test_broadcaster_skips_disabled_events():
    """Test that HookEventBroadcaster skips events not in broadcast_events list."""
    config = DaemonConfig()
    config.hook_extensions = HookExtensionsConfig(
        websocket=WebSocketBroadcastConfig(
            enabled=True,
            broadcast_events=["pre-tool-use"],  # Only pre-tool-use enabled
            include_payload=True,
        )
    )

    mock_ws = AsyncMock()
    broadcaster = HookEventBroadcaster(websocket_server=mock_ws, config=config)

    # Create a session-start event (not in broadcast_events list)
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        data={},
    )

    await broadcaster.broadcast_event(event)

    # Should NOT have been called since session-start is not enabled
    mock_ws.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_broadcaster_handles_no_websocket_server():
    """Test that HookEventBroadcaster gracefully handles missing websocket server."""
    config = DaemonConfig()
    config.hook_extensions = HookExtensionsConfig(
        websocket=WebSocketBroadcastConfig(
            enabled=True,
            broadcast_events=["session-start"],
        )
    )

    # No websocket server
    broadcaster = HookEventBroadcaster(websocket_server=None, config=config)

    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        data={},
    )

    result = await broadcaster.broadcast_event(event)
    assert result is None
    assert broadcaster.websocket_server is None
