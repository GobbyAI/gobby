"""Tests for local runtime broker routes."""

from __future__ import annotations

import os
import stat

import pytest
from fastapi.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.config.local_cli_token import ensure_local_cli_token, local_cli_token_path
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def test_local_cli_token_is_stable_and_private(tmp_path) -> None:
    token = ensure_local_cli_token(tmp_path)
    assert token
    assert ensure_local_cli_token(tmp_path) == token

    token_path = local_cli_token_path(tmp_path)
    assert token_path.read_text(encoding="utf-8").strip() == token
    if os.name != "nt":
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_database_url_endpoint_returns_daemon_dsn_and_no_store(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    token = ensure_local_cli_token()
    server = create_http_server(
        config=DaemonConfig(database_url="postgresql://gobby:secret@localhost/gobby")
    )
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    response = client.post(
        "/api/local/runtime/database-url",
        headers={"X-Gobby-Local-Token": token},
    )

    assert response.status_code == 200
    assert response.json() == {"database_url": "postgresql://gobby:secret@localhost/gobby"}
    assert response.headers["Cache-Control"] == "no-store"


def test_database_url_endpoint_rejects_missing_and_bad_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    ensure_local_cli_token()
    server = create_http_server(config=DaemonConfig(database_url="postgresql://localhost/gobby"))
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    missing = client.post("/api/local/runtime/database-url")
    bad = client.post(
        "/api/local/runtime/database-url",
        headers={"X-Gobby-Local-Token": "wrong"},
    )

    assert missing.status_code == 401
    assert bad.status_code == 401


def test_database_url_endpoint_rejects_non_loopback_client(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    token = ensure_local_cli_token()
    server = create_http_server(config=DaemonConfig(database_url="postgresql://localhost/gobby"))
    client = TestClient(server.app, client=("10.0.0.5", 50000))

    response = client.post(
        "/api/local/runtime/database-url",
        headers={"X-Gobby-Local-Token": token},
    )

    assert response.status_code == 403
