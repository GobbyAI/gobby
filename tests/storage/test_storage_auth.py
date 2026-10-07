"""Tests for browser-session storage and bootstrap-backed auth headers."""

import hashlib
from pathlib import Path

import pytest

from gobby.storage.auth import (
    AuthStore,
    hash_token,
)
from gobby.storage.config_store import is_secret_key_name
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import (
    GOBBY_AGENT_API_TOKEN_ENV,
    daemon_auth_headers,
    daemon_bootstrap_path,
    read_local_api_token,
)
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def auth_store(db: HubDatabase) -> AuthStore:
    return AuthStore(db)


@pytest.fixture
def local_token_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr("gobby.utils.local_token._daemon_bootstrap", tmp_path / "bootstrap.yaml")


def test_local_token_helpers_return_bearer_header(local_token_home: None) -> None:
    assert read_local_api_token() is None
    assert daemon_auth_headers() == {}

    daemon_bootstrap_path().write_text("api_key: '  local-token '\n")

    assert read_local_api_token() == "local-token"
    assert daemon_auth_headers() == {"Authorization": "Bearer local-token"}


def test_daemon_auth_headers_prefer_agent_capability(
    local_token_home: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon_bootstrap_path().write_text("api_key: operator-token\n")
    monkeypatch.setenv(GOBBY_AGENT_API_TOKEN_ENV, "scoped-agent-token")
    for name in ("GOBBY_SESSION_ID", "GOBBY_PROJECT_ID", "GOBBY_AGENT_RUN_ID"):
        monkeypatch.delenv(name, raising=False)

    assert read_local_api_token() == "operator-token"
    assert daemon_auth_headers() == {"Authorization": "Bearer scoped-agent-token"}


def test_daemon_auth_headers_carry_spawn_identity(
    local_token_home: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GOBBY_AGENT_API_TOKEN_ENV, "scoped-agent-token")
    monkeypatch.setenv("GOBBY_SESSION_ID", "spawn-session-uuid")
    monkeypatch.setenv("GOBBY_PROJECT_ID", "spawn-project")
    monkeypatch.setenv("GOBBY_AGENT_RUN_ID", "run-123")

    assert daemon_auth_headers() == {
        "Authorization": "Bearer scoped-agent-token",
        "X-Gobby-Session-Id": "spawn-session-uuid",
        "X-Gobby-Caller-Project-Id": "spawn-project",
        "X-Gobby-Agent-Run-Id": "run-123",
    }

    # Operator-token callers never attach spawn identity.
    monkeypatch.delenv(GOBBY_AGENT_API_TOKEN_ENV)
    daemon_bootstrap_path().write_text("api_key: operator-token\n")
    assert daemon_auth_headers() == {"Authorization": "Bearer operator-token"}


class TestAuthStoreCreateSession:
    def test_create_session_returns_token_and_expiry(self, auth_store: AuthStore) -> None:
        token, expires_at = auth_store.create_session(TEST_USER_ID)
        assert isinstance(token, str)
        assert len(token) == 64  # 32 bytes hex
        assert expires_at is not None

    def test_create_session_stores_only_token_hash(self, db: HubDatabase) -> None:
        auth_store = AuthStore(db)
        token, _ = auth_store.create_session(TEST_USER_ID)

        columns = {
            row["column_name"]
            for row in db.fetchall(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                ("auth_sessions",),
            )
        }
        row = db.fetchone("SELECT id, user_id, token_hash FROM auth_sessions")

        assert "token" not in columns
        assert row is not None
        assert str(row["user_id"]) == TEST_USER_ID
        assert row["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()

    def test_remember_me_extends_expiry(self, auth_store: AuthStore) -> None:
        _, short_exp = auth_store.create_session(TEST_USER_ID, remember_me=False)
        _, long_exp = auth_store.create_session(TEST_USER_ID, remember_me=True)
        assert long_exp > short_exp


class TestAuthStoreValidateSession:
    def test_valid_session(self, auth_store: AuthStore) -> None:
        token, _ = auth_store.create_session(TEST_USER_ID)
        assert auth_store.validate_session(token) is True

    def test_invalid_token(self, auth_store: AuthStore) -> None:
        assert auth_store.validate_session("nonexistent") is False

    def test_empty_token(self, auth_store: AuthStore) -> None:
        assert auth_store.validate_session("") is False


def test_session_user_id_resolves_only_unexpired_sessions(db: HubDatabase) -> None:
    auth_store = AuthStore(db)
    token, _ = auth_store.create_session(TEST_USER_ID)
    assert auth_store.session_user_id(token) == TEST_USER_ID
    assert auth_store.session_user_id("not-a-session") is None
    assert auth_store.session_user_id("") is None

    db.execute(
        "UPDATE auth_sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE token_hash = %s",
        (hash_token(token),),
    )
    assert auth_store.session_user_id(token) is None


class TestAuthStoreDeleteSession:
    def test_delete_invalidates(self, auth_store: AuthStore) -> None:
        token, _ = auth_store.create_session(TEST_USER_ID)
        assert auth_store.validate_session(token) is True
        auth_store.delete_session(token)
        assert auth_store.validate_session(token) is False


class TestAuthStoreExpiry:
    def test_expired_session_is_invalid(self, db: HubDatabase) -> None:
        auth_store = AuthStore(db)
        token, _ = auth_store.create_session(TEST_USER_ID)
        # Manually expire the session
        db.execute(
            """
            UPDATE auth_sessions
            SET expires_at = '2000-01-01T00:00:00+00:00'
            WHERE token_hash = %s
            """,
            (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
        )
        assert auth_store.validate_session(token) is False


class TestSecretKeyDetection:
    """Regression tests for is_secret_key_name covering auth.password."""

    def test_auth_password_is_secret(self) -> None:
        assert is_secret_key_name("auth.password") is True

    def test_underscore_password_is_secret(self) -> None:
        assert is_secret_key_name("db.admin_password") is True

    def test_api_key_is_secret(self) -> None:
        assert is_secret_key_name("service.provider_api_key") is True

    def test_normal_key_is_not_secret(self) -> None:
        assert is_secret_key_name("auth.username") is False

    def test_bare_password_is_secret(self) -> None:
        assert is_secret_key_name("password") is True
