"""Password-free sessions require verified credentials and one-time links."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.servers.http import HTTPServer
from gobby.storage.auth import AuthStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.fixtures.postgres import TEST_USER_ID
from tests.servers.conftest import (
    TEST_FRONT_DOOR_IDENTITY,
    TEST_FRONT_DOOR_SECRET,
    create_http_server,
)


@pytest.fixture
def sign_in_server(hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> HTTPServer:
    monkeypatch.setattr(
        "gobby.utils.machine_id._cached_machine_id", "21000000-0000-4000-8000-000000000001"
    )
    server = create_http_server(
        config=DaemonConfig(),
        database=hub_db,
        authenticated_requests=False,
        session_manager=SessionManager(hub_db),
        task_manager=LocalTaskManager(hub_db),
    )
    server.auth_service.bind_runtime(
        grant_service=None,
        local_machine_id=None,
        clock=None,
        front_door_secret=TEST_FRONT_DOOR_SECRET,
    )
    server.app.router.lifespan_context = FastAPI().router.lifespan_context
    return server


def test_operator_link_is_single_use_short_lived_revocable_and_audited(
    sign_in_server: HTTPServer, hub_db: HubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="gobby.servers.routes.browser_sign_in")
    with TestClient(
        sign_in_server.app, base_url="http://127.0.0.1:43210", client=("127.0.0.1", 50000)
    ) as client:
        minted = client.post(
            "/api/auth/sign-in-link",
            headers=(TEST_FRONT_DOOR_IDENTITY | {"X-Gobby-User-Id": TEST_USER_ID}),
        )
        assert minted.status_code == 200
        url = minted.json()["url"]
        assert urlsplit(url).netloc == "127.0.0.1:43210"
        assert minted.headers["cache-control"] == "no-store"
        consumed = client.get(url, follow_redirects=False)
        assert consumed.status_code == 303
        assert consumed.headers["location"] == "/"
        assert consumed.headers["referrer-policy"] == "no-referrer"
        cookie = consumed.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "Max-Age=3600" in cookie
        session_token = client.cookies["gobby_session"]
        store = AuthStore(hub_db)
        assert store.session_user_id(session_token) == TEST_USER_ID
        row = hub_db.fetchone(
            "SELECT id, expires_at FROM auth_sessions WHERE user_id = %s", (TEST_USER_ID,)
        )
        assert row is not None
        assert timedelta(minutes=59) < row["expires_at"] - datetime.now(UTC) <= timedelta(hours=1)
        assert client.get(url, follow_redirects=False).status_code == 401
        assert client.get("/api/auth/status").json() == {"authenticated": True}
        assert client.post("/api/auth/logout").status_code == 200
        assert not store.validate_session(session_token)
        assert "browser_sign_in_consumed" in caplog.text
        assert TEST_USER_ID in caplog.text
        assert str(row["id"]) in caplog.text
        assert urlsplit(url).query.split("=", 1)[1] not in caplog.text
        assert session_token not in caplog.text


@pytest.mark.parametrize("host", ["example.com", "127.0.0.1.example.com", "192.168.1.2"])
def test_non_loopback_origin_cannot_mint_or_consume(sign_in_server: HTTPServer, host: str) -> None:
    with TestClient(
        sign_in_server.app, base_url="http://127.0.0.1:43210", client=("127.0.0.1", 50000)
    ) as client:
        minted = client.post(
            "/api/auth/sign-in-link",
            headers=(TEST_FRONT_DOOR_IDENTITY | {"X-Gobby-User-Id": TEST_USER_ID}),
        )
        assert minted.status_code == 200
        url = minted.json()["url"]
        assert (
            client.post(
                "/api/auth/sign-in-link", headers=TEST_FRONT_DOOR_IDENTITY | {"Host": host}
            ).status_code
            == 403
        )
        assert client.get(url, headers={"Host": host}, follow_redirects=False).status_code == 403
        assert client.get(url, follow_redirects=False).status_code == 303


def test_cookie_and_unverified_identity_cannot_mint(
    sign_in_server: HTTPServer, hub_db: HubDatabase
) -> None:
    with TestClient(
        sign_in_server.app, base_url="http://127.0.0.1:43210", client=("127.0.0.1", 50000)
    ) as client:
        assert client.post("/api/auth/sign-in-link").status_code == 401
        cookie, _ = AuthStore(hub_db).create_session(TEST_USER_ID)
        client.cookies.set("gobby_session", cookie)
        assert client.post("/api/auth/sign-in-link").status_code == 401
        forged = {"X-Gobby-User-Id": TEST_USER_ID, "Authorization": "Bearer invented"}
        assert client.post("/api/auth/sign-in-link", headers=forged).status_code == 401


def test_expired_link_is_refused(
    sign_in_server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(
        sign_in_server.app, base_url="http://127.0.0.1:43210", client=("127.0.0.1", 50000)
    ) as client:
        minted = client.post(
            "/api/auth/sign-in-link",
            headers=(TEST_FRONT_DOOR_IDENTITY | {"X-Gobby-User-Id": TEST_USER_ID}),
        )
        assert minted.status_code == 200
        now = time.monotonic()
        monkeypatch.setattr(
            "gobby.servers.routes.browser_sign_in.time.monotonic", lambda: now + 121
        )
        response = client.get(minted.json()["url"], follow_redirects=False)
        assert response.status_code == 401
        assert "set-cookie" not in response.headers


@pytest.fixture
def managed_headers(
    sign_in_server: HTTPServer,
    hub_db: HubDatabase,
    tmp_path: Path,
    sample_project: dict[str, Any],
) -> dict[str, str]:
    from gobby.servers.auth_service import AuthService
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager
    from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token

    project_id = str(sample_project["id"])
    session = SessionManager(hub_db).register(
        external_id="browser-sign-in",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=project_id,
    )
    run = LocalAgentRunManager(hub_db).create(
        parent_session_id=session.id, provider="codex", prompt="browser proof"
    )
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text("api_key: isolated-operator-key\n")
    sign_in_server.auth_service = AuthService(lambda: hub_db, bootstrap_file=bootstrap)
    token = issue_agent_api_token(
        derive_managed_signing_key("isolated-operator-key"),
        agent_run_id=run.id,
        session_id=session.id,
        project_id=project_id,
        machine_id="21000000-0000-4000-8000-000000000001",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Session-Id": session.id,
        "X-Gobby-Project-Id": project_id,
        "X-Gobby-Agent-Run-Id": run.id,
    }


def test_managed_run_can_mint_but_dead_or_forged_run_cannot(
    sign_in_server: HTTPServer, hub_db: HubDatabase, managed_headers: dict[str, str]
) -> None:
    with TestClient(
        sign_in_server.app, base_url="http://127.0.0.1:43210", client=("127.0.0.1", 50000)
    ) as client:
        minted = client.post("/api/auth/sign-in-link", headers=managed_headers)
        assert minted.status_code == 200
        assert (
            client.post(
                "/api/auth/sign-in-link",
                headers=managed_headers | {"X-Gobby-Agent-Run-Id": "forged-run"},
            ).status_code
            == 401
        )
        hub_db.execute(
            "UPDATE agent_runs SET status = 'completed' WHERE id = %s",
            (managed_headers["X-Gobby-Agent-Run-Id"],),
        )
        assert client.post("/api/auth/sign-in-link", headers=managed_headers).status_code == 401
        assert client.get(minted.json()["url"], follow_redirects=False).status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/auth/keys"),
        ("GET", "/api/auth/keys"),
        ("POST", "/api/configuration/secrets"),
        ("PUT", "/api/configuration"),
        ("POST", "/api/projects/init"),
        ("POST", "/api/users"),
        ("POST", "/api/config/secrets"),
        ("PATCH", "/api/config/values"),
        ("DELETE", "/api/auth/keys/key-id"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/projects/{project_id}/checkouts"),
    ],
)
def test_managed_browser_refuses_operator_authority(
    sign_in_server: HTTPServer, managed_headers: dict[str, str], method: str, path: str
) -> None:
    with TestClient(
        sign_in_server.app, base_url="http://127.0.0.1:43210", client=("127.0.0.1", 50000)
    ) as client:
        minted = client.post("/api/auth/sign-in-link", headers=managed_headers)
        assert minted.status_code == 200
        assert client.get(minted.json()["url"], follow_redirects=False).status_code == 303
        target = path.replace("{project_id}", managed_headers["X-Gobby-Project-Id"])
        response = client.request(method, target, json={"label": "must-not-mint"})
        assert response.status_code == 403
        assert client.post("/api/auth/sign-in-link").status_code in {401, 403}


def test_managed_browser_revokes_on_run_end(
    sign_in_server: HTTPServer, hub_db: HubDatabase, managed_headers: dict[str, str]
) -> None:
    with TestClient(
        sign_in_server.app, base_url="http://127.0.0.1:43210", client=("127.0.0.1", 50000)
    ) as client:
        minted = client.post("/api/auth/sign-in-link", headers=managed_headers)
        assert minted.status_code == 200
        assert client.get(minted.json()["url"], follow_redirects=False).status_code == 303
        assert client.get("/api/auth/status").json() == {"authenticated": True}
        hub_db.execute(
            "UPDATE agent_runs SET status = 'completed' WHERE id = %s",
            (managed_headers["X-Gobby-Agent-Run-Id"],),
        )
        assert client.get("/api/auth/status").json() == {"authenticated": False}
        assert client.get("/api/tasks").status_code == 401


def test_managed_browser_has_distinct_cookie_and_project_context(
    sign_in_server: HTTPServer, hub_db: HubDatabase, managed_headers: dict[str, str]
) -> None:
    from gobby.storage.auth import hash_token

    with TestClient(
        sign_in_server.app, base_url="http://127.0.0.1:43210", client=("127.0.0.1", 50000)
    ) as client:
        minted = client.post("/api/auth/sign-in-link", headers=managed_headers)
        assert minted.status_code == 200
        assert client.get(minted.json()["url"], follow_redirects=False).status_code == 303
        token = client.cookies["gobby_session"]
        assert (
            hub_db.fetchone(
                "SELECT id FROM auth_sessions WHERE token_hash = %s", (hash_token(token),)
            )
            is None
        )
        assert AuthStore(hub_db).session_user_id(token) is None
        assert client.get("/api/tasks").status_code == 200
        assert (
            client.get(
                "/api/tasks", params={"project_id": "00000000-0000-4000-8000-000000000001"}
            ).status_code
            == 403
        )
        assert client.post("/api/auth/sign-in-link").status_code in {401, 403}
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/auth/status").json() == {"authenticated": False}
        client.cookies.set("gobby_session", token)
        assert client.get("/api/auth/status").json() == {"authenticated": False}


def test_managed_browser_can_replace_its_cookie_with_a_new_link(
    sign_in_server: HTTPServer, managed_headers: dict[str, str]
) -> None:
    with TestClient(
        sign_in_server.app, base_url="http://127.0.0.1:43210", client=("127.0.0.1", 50000)
    ) as client:
        first = client.post("/api/auth/sign-in-link", headers=managed_headers).json()["url"]
        second = client.post("/api/auth/sign-in-link", headers=managed_headers).json()["url"]
        assert client.get(first, follow_redirects=False).status_code == 303
        assert client.get(second, follow_redirects=False).status_code == 303
        assert client.get("/api/auth/status").json() == {"authenticated": True}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/projects/00000000-0000-4000-8000-000000000001"),
        ("POST", "/api/projects/00000000-0000-4000-8000-000000000001/checkouts"),
    ],
)
def test_managed_browser_refuses_foreign_project_paths(
    sign_in_server: HTTPServer, managed_headers: dict[str, str], method: str, path: str
) -> None:
    with TestClient(
        sign_in_server.app, base_url="http://127.0.0.1:43210", client=("127.0.0.1", 50000)
    ) as client:
        link = client.post("/api/auth/sign-in-link", headers=managed_headers).json()["url"]
        assert client.get(link, follow_redirects=False).status_code == 303
        assert client.request(method, path, json={}).status_code == 403
