"""Real-app wiring coverage for attention HTTP endpoints."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.config.bootstrap import BootstrapConfig
from gobby.servers.http import HTTPServer
from gobby.storage.attention import AttentionStateManager
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def test_app_reaches_attention_endpoints(temp_db: HubDatabase) -> None:
    services = ServiceContainer(
        config=None,
        database=temp_db,
        session_manager=None,
        task_manager=MagicMock(),
        attention_manager=AttentionStateManager(temp_db, epoch="wiring-test"),
    )
    server = HTTPServer(
        services=services, test_mode=True, bootstrap_config=BootstrapConfig(auth_mode="disabled")
    )
    paths = {route.path for route in server.app.routes}
    assert "/api/attention/roster" in paths
    assert "/api/attention/{entry_id}/seen" in paths
    assert "/api/attention/{entry_id}/respond" in paths
    with TestClient(server.app) as client:
        roster = client.get("/api/attention/roster")
        seen = client.post("/api/attention/run:missing/seen", json={"attention_id": "missing"})
        respond = client.post(
            "/api/attention/run:missing/respond",
            json={
                "attention_id": "missing",
                "fingerprint": "0" * 64,
                "answer": {"key": "escape"},
            },
        )
    assert roster.status_code == 200
    assert roster.json() == {"epoch": "wiring-test", "seq": 0, "entries": []}
    assert seen.status_code == 404 and respond.status_code == 404
