"""Tests for the dormant wiki code routes."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.servers.routes.wiki_code import create_wiki_code_router
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def test_dormant_status_and_refresh() -> None:
    server = MagicMock()
    app = FastAPI()
    app.include_router(create_wiki_code_router(server))
    client = TestClient(app)

    status = client.get("/api/wiki/code/status")
    refresh = client.post("/api/wiki/code/refresh")

    assert status.status_code == 200
    assert status.json() == {
        "enabled": False,
        "state": "disabled",
        "reason": "pending_wiki_redesign",
    }
    assert refresh.status_code == 409
    assert refresh.json() == {
        "error": "codewiki_disabled_pending_redesign",
        "reason": "pending_wiki_redesign",
    }
    assert server.mock_calls == []


def test_wiki_code_router_registered_in_app() -> None:
    server = create_http_server(config=DaemonConfig())

    route_paths = {getattr(route, "path", "") for route in server.app.routes}

    assert "/api/wiki/code/status" in route_paths
    assert "/api/wiki/code/refresh" in route_paths


def test_dormant_outputs_pinned() -> None:
    server = create_http_server(config=DaemonConfig())
    client = TestClient(server.app)

    status = client.get("/api/wiki/code/status")
    refresh = client.post("/api/wiki/code/refresh")

    assert status.status_code == 200
    assert status.json() == {
        "enabled": False,
        "state": "disabled",
        "reason": "pending_wiki_redesign",
    }
    assert refresh.status_code == 409
    assert refresh.json() == {
        "error": "codewiki_disabled_pending_redesign",
        "reason": "pending_wiki_redesign",
    }
