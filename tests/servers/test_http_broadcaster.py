import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from gobby.config.app import (
    DaemonConfig,
    HookExtensionsConfig,
    WebSocketBroadcastConfig,
)
from gobby.hooks.hook_types import HookType, SessionStartInput, SessionStartOutput
from gobby.servers.http import HTTPServer


@pytest.mark.asyncio
async def test_execute_hook_broadcasts_event_client():
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

    # Setup server with real components
    server = HTTPServer(websocket_server=mock_ws, config=config, test_mode=True)

    # We need to manually trigger lifespan to create HookManager
    # OR rely on TestClient context manager.
    # TestClient(app) runs lifespan automatically.

    with TestClient(server.app) as client:
        # Patch HookManager to believe daemon is ready
        # HookManager is created in lifespan startup, which runs when we enter the context
        hook_manager = server.app.state.hook_manager
        hook_manager._get_cached_daemon_status = MagicMock(return_value=(True, {}, "running", None))

        payload = {
            "hook_type": "session-start",
            "input_data": {
                "session_id": "test-session",
                "project_path": "/tmp/test",
                "resume": False,
                "transcript_path": "/tmp/transcript.jsonl",
            },
            "source": "claude",
        }

        # Execute request
        response = client.post("/hooks/execute", json=payload)

        assert response.status_code == 200, f"Request failed: {response.text}"

        # Allow async broadcast task to run
        await asyncio.sleep(0.1)

        # Verify websocket broadcast was called
        mock_ws.broadcast.assert_called_once()

        # Verify payload content
        call_args = mock_ws.broadcast.call_args
        broadcast_payload = call_args[0][0]

        assert broadcast_payload["type"] == "hook_event"
        assert broadcast_payload["event_type"] == "session-start"
        assert broadcast_payload.get("session_id") == "test-session"
        assert "data" in broadcast_payload
