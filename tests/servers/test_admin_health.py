"""Admin health reports gterm host state (plan 3.1.7)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from gobby.servers.http import HTTPServer

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("authenticated_http_requests", "isolated_http_runtime"),
]


def test_health_reports_gterm_host_state(basic_http_server: HTTPServer) -> None:
    host = MagicMock()
    host.health_state.return_value = {
        "enabled": True,
        "running": False,
        "adopted": False,
        "host_epoch": None,
        "protocol_version": 1,
        "restart_count": 2,
        "backoff_seconds": 1.5,
        "live_terminals": 0,
        "orphaned_terminals": 3,
        "last_error": "gterm missing",
    }
    runner = MagicMock()
    runner.terminal_host_manager = host
    runner.degraded_services = {"gterm_host"}
    basic_http_server._runner = runner

    client = TestClient(basic_http_server.app)
    health = client.get("/api/health")
    assert health.status_code == 200
    payload = health.json()
    gterm = payload.get("gterm_host")
    if gterm is None:
        status = client.get("/api/admin/status")
        assert status.status_code == 200
        gterm = status.json()["gterm_host"]
    gterm_map = cast(dict[str, Any], gterm)
    assert gterm_map["enabled"] is True
    assert gterm_map["running"] is False
    assert gterm_map["protocol_version"] == 1
    assert gterm_map["restart_count"] == 2
    assert gterm_map["backoff_seconds"] == 1.5
    assert gterm_map["live_terminals"] == 0
    assert gterm_map["orphaned_terminals"] == 3
    assert gterm_map["last_error"] == "gterm missing"
    assert "gterm_host" in payload.get("degraded_services", []) or "gterm_host" in (
        client.get("/api/admin/status").json().get("degraded_services") or []
    )


def test_health_reports_run_mode(basic_http_server: HTTPServer) -> None:
    """2.1.3: /api/health reports the mode name from the bootstrap pair."""
    from gobby.config.bootstrap import BootstrapConfig

    basic_http_server.bootstrap_config = BootstrapConfig(datastore_mode="local", hub=True)
    payload = TestClient(basic_http_server.app).get("/api/health").json()
    assert payload["mode"] == "hub"

    basic_http_server.bootstrap_config = BootstrapConfig()
    payload = TestClient(basic_http_server.app).get("/api/health").json()
    assert payload["mode"] == "standalone"


def test_node_bootstrap_reports_node_mode(basic_http_server: HTTPServer) -> None:
    """2.1.4: a node bootstrap serves health with mode: node."""
    from gobby.config.bootstrap import BootstrapConfig

    basic_http_server.bootstrap_config = BootstrapConfig(
        datastore_mode="remote", hub_daemon_url="http://hub.example.test:60887"
    )
    payload = TestClient(basic_http_server.app).get("/api/health").json()
    assert payload["mode"] == "node"
