"""Live-daemon end-to-end coverage for mandatory authentication."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosedError, InvalidStatus

from gobby.identity import hash_password
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.users import LocalUserManager
from tests.e2e.conftest import DaemonInstance, daemon_token, prepare_daemon_env
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.e2e

TEST_EMAIL = "auth-e2e-user@gobby.local"
TEST_PASSWORD = "auth-e2e-password"
_MCP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "gobby-auth-e2e", "version": "1.0"},
    },
}


@pytest.fixture
def e2e_pre_daemon_setup(postgres_db: HubDatabase) -> None:
    """Seed the canonical user before the isolated daemon starts."""
    users = LocalUserManager(postgres_db)
    users.update_profile(
        TEST_USER_ID,
        name="Auth E2E User",
        email=TEST_EMAIL,
    )
    users.update_password(TEST_USER_ID, hash_password(TEST_PASSWORD))


def _hook_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "enqueued_at": "2026-07-10T12:00:00Z",
        "critical": False,
        "response_capability": "hook-response.v1",
        "hook_type": "session-start",
        "source": "claude",
        "input_data": {
            "session_id": f"auth-e2e-hook-{uuid4()}",
            "machine_id": "auth-e2e-machine",
        },
    }


def _register_session(client: httpx.Client, project_dir: Path) -> str:
    project_id = "7d5f7f2b-202a-4ca6-a06d-39dfc15b9932"
    project_response = client.post(
        "/api/admin/test/register-project",
        json={
            "project_id": project_id,
            "name": "auth-e2e-project",
            "repo_path": str(project_dir),
        },
    )
    assert project_response.is_success, project_response.text
    response = client.post(
        "/api/sessions/register",
        json={
            "external_id": f"auth-e2e-{uuid4()}",
            "machine_id": "auth-e2e-machine",
            "source": "Codex",
            "project_id": project_id,
        },
    )
    assert response.is_success, response.text
    session_id = response.json().get("id")
    assert isinstance(session_id, str)
    return session_id


def _protected_requests(session_id: str) -> list[tuple[str, str, dict[str, object] | None]]:
    return [
        ("GET", "/api/admin/config", None),
        ("GET", f"/api/sessions/{session_id}", None),
        ("POST", "/api/hooks/execute", _hook_payload()),
        ("POST", "/mcp", _MCP_INITIALIZE),
        ("POST", "/memory/dream", {"dry_run": True, "wait": True}),
    ]


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    payload: dict[str, object] | None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request_headers = dict(headers or {})
    if path.startswith("/mcp"):
        request_headers["Accept"] = "application/json, text/event-stream"
    return client.request(method, path, json=payload, headers=request_headers)


def _assert_authenticated_http_matrix(
    client: httpx.Client,
    instance: DaemonInstance,
) -> None:
    session_id = _register_session(client, instance.project_dir)
    for method, path, payload in _protected_requests(session_id):
        response = _request(client, method, path, payload)
        if path == "/memory/dream":
            assert response.status_code == 400
            assert response.json() == {
                "success": False,
                "error": "memory dream is disabled",
            }
            continue
        assert 200 <= response.status_code < 300, (path, response.status_code, response.text)


def _browser_session_cookie(instance: DaemonInstance) -> str:
    with httpx.Client(base_url=instance.http_url, timeout=10.0) as client:
        response = client.post(
            "/api/auth/login",
            json={
                "email": TEST_EMAIL,
                "password": TEST_PASSWORD,
                "remember_me": False,
            },
        )
        response.raise_for_status()
        cookie = client.cookies.get("gobby_session")
    assert cookie is not None
    return cookie


async def _receive_json(websocket: ClientConnection) -> dict[str, object]:
    raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
    assert isinstance(raw, str)
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


async def _assert_websocket_frames(
    url: str,
    *,
    headers: list[tuple[str, str]] | None = None,
) -> None:
    async with websockets.connect(
        url,
        additional_headers=headers,
        open_timeout=5.0,
        close_timeout=2.0,
    ) as websocket:
        welcome = await _receive_json(websocket)
        assert welcome["type"] == "connection_established"
        await websocket.send(json.dumps({"type": "ping"}))
        pong = await _receive_json(websocket)
        assert pong["type"] == "pong"


async def _assert_handshake_rejected(
    url: str,
    *,
    headers: list[tuple[str, str]] | None = None,
    status_code: int,
) -> None:
    with pytest.raises(InvalidStatus) as raised:
        async with websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=5.0,
            close_timeout=2.0,
        ):
            pass
    assert raised.value.response.status_code == status_code


def _rotate_isolated_token(instance: DaemonInstance) -> tuple[str, str]:
    old_token = daemon_token(instance.gobby_home)
    env = prepare_daemon_env(home_dir=instance.gobby_home)
    env["GOBBY_HOME"] = str(instance.gobby_home)
    env["GOBBY_CONFIG"] = str(instance.config_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gobby.cli import cli; cli()",
            "auth",
            "token",
            "--rotate",
        ],
        cwd=instance.project_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Local API token rotated." in completed.stdout
    new_token = daemon_token(instance.gobby_home)
    assert new_token != old_token
    return old_token, new_token


def _wait_for_old_token_rejection(instance: DaemonInstance, old_token: str) -> None:
    deadline = time.monotonic() + 8.0
    with httpx.Client(base_url=instance.http_url, timeout=5.0) as client:
        while time.monotonic() < deadline:
            response = client.get(
                "/api/admin/config",
                headers={"Authorization": f"Bearer {old_token}"},
            )
            if response.status_code == 401:
                return
            assert response.status_code == 200
            time.sleep(0.2)
    pytest.fail("old bearer remained valid beyond the AuthService refresh window")


def test_http_auth_matrix(daemon_instance: DaemonInstance) -> None:
    token = daemon_token(daemon_instance.gobby_home)
    unauthenticated_session = str(uuid4())

    with httpx.Client(
        base_url=daemon_instance.http_url,
        timeout=15.0,
        follow_redirects=True,
    ) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/mcp", json=_MCP_INITIALIZE).status_code == 401
        for method, path, payload in _protected_requests(unauthenticated_session):
            assert _request(client, method, path, payload).status_code == 401

        invalid = client.get(
            "/api/admin/config",
            headers={"Authorization": "Bearer garbage-token"},
        )
        assert invalid.status_code == 401

        local_header = client.get(
            "/api/admin/config",
            headers={"X-Gobby-Local-Token": token},
        )
        assert local_header.status_code == 200

    with httpx.Client(
        base_url=daemon_instance.http_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=15.0,
        follow_redirects=True,
    ) as authenticated:
        _assert_authenticated_http_matrix(authenticated, daemon_instance)
        assert _request(authenticated, "POST", "/mcp/mcp", _MCP_INITIALIZE).status_code == 404


@pytest.mark.asyncio
async def test_ws_auth(daemon_instance: DaemonInstance) -> None:
    token = daemon_token(daemon_instance.gobby_home)
    await _assert_handshake_rejected(daemon_instance.ws_url, status_code=401)
    await _assert_websocket_frames(
        daemon_instance.ws_url,
        headers=[("Authorization", f"Bearer {token}")],
    )

    async with websockets.connect(
        f"ws://localhost:{daemon_instance.http_port}/ws",
        open_timeout=5.0,
        close_timeout=2.0,
    ) as websocket:
        with pytest.raises(ConnectionClosedError) as http_ws_closed:
            await websocket.recv()
    assert http_ws_closed.value.code == 4401
    cookie = _browser_session_cookie(daemon_instance)
    await _assert_websocket_frames(
        f"ws://localhost:{daemon_instance.http_port}/ws",
        headers=[("Cookie", f"gobby_session={cookie}")],
    )


def test_token_rotation(daemon_instance: DaemonInstance) -> None:
    old_token, new_token = _rotate_isolated_token(daemon_instance)
    _wait_for_old_token_rejection(daemon_instance, old_token)

    response = httpx.get(
        f"{daemon_instance.http_url}/api/admin/config",
        headers={"Authorization": f"Bearer {new_token}"},
        timeout=5.0,
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_ws_rotation(daemon_instance: DaemonInstance) -> None:
    cookie = _browser_session_cookie(daemon_instance)
    old_token, new_token = _rotate_isolated_token(daemon_instance)
    _wait_for_old_token_rejection(daemon_instance, old_token)

    await _assert_handshake_rejected(
        daemon_instance.ws_url,
        headers=[("Authorization", f"Bearer {old_token}")],
        status_code=403,
    )
    await _assert_websocket_frames(
        daemon_instance.ws_url,
        headers=[("Authorization", f"Bearer {new_token}")],
    )
    await _assert_websocket_frames(
        f"ws://localhost:{daemon_instance.http_port}/ws",
        headers=[("Cookie", f"gobby_session={cookie}")],
    )
