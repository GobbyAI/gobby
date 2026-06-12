"""Tests for communications HTTP routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.communications.models import CommsMessage
from gobby.servers.http import HTTPServer
from gobby.servers.routes.communications import create_communications_router


def test_receive_webhook_returns_discord_interaction_ping_ack() -> None:
    message = CommsMessage(
        id="discord_ping_1",
        channel_id="test_discord_channel",
        direction="inbound",
        content='{"type": 1}',
        created_at="2024-01-01T00:00:00Z",
        content_type="interaction_ping",
    )
    comms_manager = SimpleNamespace(handle_inbound=AsyncMock(return_value=[message]))
    server = SimpleNamespace(services=SimpleNamespace(communications_manager=comms_manager))
    app = FastAPI()
    app.include_router(create_communications_router(cast(HTTPServer, server)))

    response = TestClient(app).post("/api/comms/webhooks/discord", json={"type": 1})

    assert response.status_code == 200
    assert response.json() == {"type": 1}
    comms_manager.handle_inbound.assert_awaited_once()
