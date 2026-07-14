"""Tests for auth routes — login, logout, status.

Uses real DaemonConfig + real temp_db (no LLM mocking needed).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.auth import hash_password
from gobby.storage.config_store import ConfigStore
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db(hub_db):
    return hub_db


@pytest.fixture
def task_manager(temp_db):
    return LocalTaskManager(temp_db)


@pytest.fixture
def config_with_auth() -> DaemonConfig:
    """Config with auth username set (password stored separately in secrets)."""
    return DaemonConfig(auth={"username": "testuser", "password": ""})


@pytest.fixture
def config_no_auth() -> DaemonConfig:
    return DaemonConfig()


def _setup_auth_password(db, password: str = "correctpassword") -> None:
    """Store web credentials in the shared auth service's config keys."""
    config_store = ConfigStore(db)
    config_store.set("auth.username", "testuser", source="user")
    config_store.set("auth.password_hash", hash_password(password), source="user")


# ---------------------------------------------------------------------------
# GET /api/auth/status
# ---------------------------------------------------------------------------


class TestAuthStatus:
    def test_status_credentials_configured(self, temp_db, config_no_auth, task_manager) -> None:
        _setup_auth_password(temp_db)
        server = create_http_server(
            config=config_no_auth,
            database=temp_db,
            task_manager=task_manager,
            auth_mode="required",
        )

        response = TestClient(server.app).get("/api/auth/status")

        assert response.status_code == 200
        assert response.json() == {
            "auth_required": True,
            "authenticated": False,
            "credentials_configured": True,
        }

    def test_auth_not_required_when_unconfigured(
        self, temp_db, config_no_auth, task_manager
    ) -> None:
        server = create_http_server(
            config=config_no_auth, database=temp_db, task_manager=task_manager
        )
        client = TestClient(server.app)
        resp = client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_required"] is False
        assert data["authenticated"] is True

    def test_auth_required_when_configured(self, temp_db, config_with_auth, task_manager) -> None:
        _setup_auth_password(temp_db)
        server = create_http_server(
            config=config_with_auth,
            database=temp_db,
            task_manager=task_manager,
            auth_mode="required",
        )
        client = TestClient(server.app)
        resp = client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_required"] is True
        assert data["authenticated"] is False

    def test_auth_required_with_hub_database_protocol(
        self, non_local_hub_db, config_with_auth
    ) -> None:
        _setup_auth_password(non_local_hub_db)
        server = create_http_server(
            config=config_with_auth,
            database=non_local_hub_db,
            task_manager=LocalTaskManager(non_local_hub_db),
            auth_mode="required",
        )
        client = TestClient(server.app)

        resp = client.get("/api/auth/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_required"] is True
        assert data["authenticated"] is False


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------


class TestAuthLogin:
    def test_login_success(self, temp_db, config_with_auth, task_manager) -> None:
        _setup_auth_password(temp_db, "mypassword")
        server = create_http_server(
            config=config_with_auth,
            database=temp_db,
            task_manager=task_manager,
            auth_mode="required",
        )
        client = TestClient(server.app)
        resp = client.post(
            "/api/auth/login", json={"username": "testuser", "password": "mypassword"}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "gobby_session" in resp.cookies

    def test_login_success_with_hub_database_protocol(
        self, non_local_hub_db, config_with_auth
    ) -> None:
        _setup_auth_password(non_local_hub_db, "mypassword")
        server = create_http_server(
            config=config_with_auth,
            database=non_local_hub_db,
            task_manager=LocalTaskManager(non_local_hub_db),
        )
        client = TestClient(server.app)

        resp = client.post(
            "/api/auth/login", json={"username": "testuser", "password": "mypassword"}
        )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "gobby_session" in resp.cookies

    def test_login_wrong_password(self, temp_db, config_with_auth, task_manager) -> None:
        _setup_auth_password(temp_db, "mypassword")
        server = create_http_server(
            config=config_with_auth, database=temp_db, task_manager=task_manager
        )
        client = TestClient(server.app)
        resp = client.post("/api/auth/login", json={"username": "testuser", "password": "wrong"})
        assert resp.status_code == 401
        assert resp.json()["ok"] is False

    def test_login_wrong_username(self, temp_db, config_with_auth, task_manager) -> None:
        _setup_auth_password(temp_db, "mypassword")
        server = create_http_server(
            config=config_with_auth, database=temp_db, task_manager=task_manager
        )
        client = TestClient(server.app)
        resp = client.post("/api/auth/login", json={"username": "wrong", "password": "mypassword"})
        assert resp.status_code == 401

    def test_repeated_failed_logins_are_locked_out(
        self, temp_db, config_with_auth, task_manager
    ) -> None:
        _setup_auth_password(temp_db, "mypassword")
        server = create_http_server(
            config=config_with_auth, database=temp_db, task_manager=task_manager
        )
        client = TestClient(server.app)
        credentials = {"username": "testuser", "password": "wrong"}

        for _ in range(5):
            assert client.post("/api/auth/login", json=credentials).status_code == 401

        response = client.post("/api/auth/login", json=credentials)

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert response.json() == {"ok": False, "error": "Too many failed login attempts"}

    def test_successful_login_resets_failed_attempts(
        self, temp_db, config_with_auth, task_manager
    ) -> None:
        _setup_auth_password(temp_db, "mypassword")
        server = create_http_server(
            config=config_with_auth, database=temp_db, task_manager=task_manager
        )
        client = TestClient(server.app)
        wrong_credentials = {"username": "testuser", "password": "wrong"}

        for _ in range(4):
            assert client.post("/api/auth/login", json=wrong_credentials).status_code == 401

        response = client.post(
            "/api/auth/login",
            json={"username": "testuser", "password": "mypassword"},
        )
        assert response.status_code == 200

        for _ in range(5):
            assert client.post("/api/auth/login", json=wrong_credentials).status_code == 401

    def test_login_when_not_configured(self, temp_db, config_no_auth, task_manager) -> None:
        server = create_http_server(
            config=config_no_auth, database=temp_db, task_manager=task_manager
        )
        client = TestClient(server.app)
        resp = client.post("/api/auth/login", json={"username": "any", "password": "any"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/auth/logout
# ---------------------------------------------------------------------------


class TestAuthLogout:
    def test_logout_clears_session(self, temp_db, config_with_auth, task_manager) -> None:
        _setup_auth_password(temp_db, "mypassword")
        server = create_http_server(
            config=config_with_auth,
            database=temp_db,
            task_manager=task_manager,
            auth_mode="required",
        )
        client = TestClient(server.app)

        # Login first
        login_resp = client.post(
            "/api/auth/login", json={"username": "testuser", "password": "mypassword"}
        )
        assert login_resp.status_code == 200

        # Verify authenticated
        status_resp = client.get("/api/auth/status")
        assert status_resp.json()["authenticated"] is True

        # Logout
        logout_resp = client.post("/api/auth/logout")
        assert logout_resp.status_code == 200
        assert logout_resp.json()["ok"] is True

        # Verify no longer authenticated (fresh client, no cookies)
        fresh_client = TestClient(server.app)
        status_resp = fresh_client.get("/api/auth/status")
        assert status_resp.json()["authenticated"] is False
