"""Tests for canonical-user login, logout, and status routes."""

from __future__ import annotations

from typing import cast
from unittest.mock import patch

import pytest
from httpx2 import Response
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.identity import DUMMY_PASSWORD_HASH, hash_password, verify_password_hash
from gobby.servers.http import HTTPServer
from gobby.storage.auth import hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.users import LocalUserManager
from tests.conftest import NonLocalHubDatabase
from tests.fixtures.postgres import TEST_USER_EMAIL, TEST_USER_ID
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db(hub_db: HubDatabase) -> HubDatabase:
    return hub_db


def _server(db: HubDatabase) -> HTTPServer:
    return create_http_server(
        config=DaemonConfig(),
        database=db,
        task_manager=LocalTaskManager(db),
        authenticated_requests=False,
    )


def _set_password(db: HubDatabase, password: str = "correctpassword") -> None:
    LocalUserManager(db).update_password(TEST_USER_ID, hash_password(password))


def _login(client: TestClient, *, email: str = TEST_USER_EMAIL, password: str) -> Response:
    return client.post(
        "/api/auth/login",
        json={"email": email, "password": password, "remember_me": False},
    )


class TestAuthStatus:
    def test_status_reports_unauthenticated_request(self, temp_db: HubDatabase) -> None:
        response = TestClient(_server(temp_db).app).get("/api/auth/status")

        assert response.status_code == 200
        assert response.json() == {"authenticated": False}

    def test_status_accepts_user_owned_session(self, temp_db: HubDatabase) -> None:
        _set_password(temp_db)
        client = TestClient(_server(temp_db).app)
        assert _login(client, password="correctpassword").status_code == 200

        response = client.get("/api/auth/status")

        assert response.json() == {"authenticated": True}


class TestAuthLogin:
    def test_login_is_case_insensitive_and_creates_user_owned_session(
        self, temp_db: HubDatabase
    ) -> None:
        _set_password(temp_db, "mypassword")
        client = TestClient(_server(temp_db).app)

        response = _login(
            client,
            email=f"  {TEST_USER_EMAIL.upper()}  ",
            password="mypassword",
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        token = response.cookies["gobby_session"]
        row = temp_db.fetchone(
            "SELECT user_id FROM auth_sessions WHERE token_hash = %s",
            (hash_token(token),),
        )
        assert row is not None
        assert row["user_id"] == TEST_USER_ID

    def test_login_works_with_hub_database_protocol(
        self, non_local_hub_db: NonLocalHubDatabase
    ) -> None:
        db = cast(HubDatabase, non_local_hub_db)
        _set_password(db, "mypassword")

        response = _login(TestClient(_server(db).app), password="mypassword")

        assert response.status_code == 200
        assert "gobby_session" in response.cookies

    def test_wrong_password_and_unknown_email_share_response_and_argon2_work(
        self,
        temp_db: HubDatabase,
    ) -> None:
        _set_password(temp_db, "mypassword")
        client = TestClient(_server(temp_db).app)

        with patch(
            "gobby.servers.auth_service.verify_password_hash",
            wraps=verify_password_hash,
        ) as verify:
            wrong_password = _login(client, password="wrong")
            unknown_email = _login(
                client,
                email="missing@example.com",
                password="wrong",
            )
            blank_email = _login(client, email=" ", password="wrong")
            malformed_email = _login(client, email="invalid", password="wrong")

        assert wrong_password.status_code == 401
        assert unknown_email.status_code == 401
        assert blank_email.status_code == 401
        assert malformed_email.status_code == 401
        expected = {"ok": False, "error": "Invalid email or password"}
        assert wrong_password.json() == expected
        assert unknown_email.json() == expected
        assert blank_email.json() == expected
        assert malformed_email.json() == expected
        assert verify.call_count == 4
        assert all(call.args[1] == DUMMY_PASSWORD_HASH for call in verify.call_args_list[1:])

    @pytest.mark.parametrize("email", [TEST_USER_EMAIL, " ", "invalid"])
    def test_repeated_failed_logins_are_locked_out(self, temp_db: HubDatabase, email: str) -> None:
        _set_password(temp_db, "mypassword")
        client = TestClient(_server(temp_db).app)
        credentials = {"email": email, "password": "wrong"}

        for _ in range(5):
            assert client.post("/api/auth/login", json=credentials).status_code == 401

        response = client.post("/api/auth/login", json=credentials)

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"

    def test_tailscale_proxy_tracks_login_failures_per_user(self, temp_db: HubDatabase) -> None:
        _set_password(temp_db, "mypassword")
        server = _server(temp_db)
        server.bootstrap_config = BootstrapConfig(ui_expose="tailscale")
        client = TestClient(server.app, client=("127.0.0.1", 50000))
        credentials = {"email": TEST_USER_EMAIL, "password": "wrong"}

        for _ in range(5):
            response = client.post(
                "/api/auth/login",
                json=credentials,
                headers={"Tailscale-User-Login": "alice@example.com"},
            )
            assert response.status_code == 401

        alice = client.post(
            "/api/auth/login",
            json=credentials,
            headers={"Tailscale-User-Login": "alice@example.com"},
        )
        bob = client.post(
            "/api/auth/login",
            json=credentials,
            headers={"Tailscale-User-Login": "bob@example.com"},
        )

        assert alice.status_code == 429
        assert bob.status_code == 401

    def test_tailscale_identity_header_requires_loopback_proxy(self, temp_db: HubDatabase) -> None:
        _set_password(temp_db, "mypassword")
        server = _server(temp_db)
        server.bootstrap_config = BootstrapConfig(ui_expose="tailscale")
        client = TestClient(server.app, client=("203.0.113.10", 50000))
        credentials = {"email": TEST_USER_EMAIL, "password": "wrong"}

        for attempt in range(5):
            response = client.post(
                "/api/auth/login",
                json=credentials,
                headers={"Tailscale-User-Login": f"spoofed-{attempt}@example.com"},
            )
            assert response.status_code == 401

        response = client.post(
            "/api/auth/login",
            json=credentials,
            headers={"Tailscale-User-Login": "fresh-spoof@example.com"},
        )

        assert response.status_code == 429

    def test_successful_login_resets_failed_attempts(self, temp_db: HubDatabase) -> None:
        _set_password(temp_db, "mypassword")
        client = TestClient(_server(temp_db).app)
        wrong = {"email": TEST_USER_EMAIL, "password": "wrong"}

        for _ in range(4):
            assert client.post("/api/auth/login", json=wrong).status_code == 401

        assert _login(client, password="mypassword").status_code == 200
        for _ in range(5):
            assert client.post("/api/auth/login", json=wrong).status_code == 401


class TestAuthLogout:
    def test_logout_clears_session(self, temp_db: HubDatabase) -> None:
        _set_password(temp_db, "mypassword")
        server = _server(temp_db)
        client = TestClient(server.app)
        assert _login(client, password="mypassword").status_code == 200
        assert client.get("/api/auth/status").json()["authenticated"] is True

        response = client.post("/api/auth/logout")

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert TestClient(server.app).get("/api/auth/status").json() == {"authenticated": False}
