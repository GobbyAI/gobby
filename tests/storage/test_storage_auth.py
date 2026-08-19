"""Tests for authentication storage and local API token provisioning."""

import hashlib
import logging
import stat
from pathlib import Path

import pytest

from gobby.storage.auth import (
    LOCAL_API_TOKEN_HASH_KEY,
    AuthStore,
    ensure_local_api_token,
    hash_token,
    rotate_local_api_token,
)
from gobby.storage.config_store import is_secret_key_name
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import (
    GOBBY_AGENT_API_TOKEN_ENV,
    daemon_auth_headers,
    local_token_path,
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


def test_local_api_token_hash_missing(auth_store: AuthStore) -> None:
    assert auth_store.get_local_api_token_hash() is None


def test_local_api_token_hash_write_uses_system_source(
    auth_store: AuthStore,
    db: HubDatabase,
) -> None:
    auth_store.set_local_api_token_hash("stored-hash")

    row = db.fetchone(
        "SELECT source FROM config_store WHERE key = %s",
        (LOCAL_API_TOKEN_HASH_KEY,),
    )

    assert auth_store.get_local_api_token_hash() == "stored-hash"
    assert row is not None
    assert row["source"] == "system"


@pytest.mark.parametrize("stored_json", ["123", '""'])
def test_local_api_token_hash_invalid_values_fail_closed(
    auth_store: AuthStore,
    db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
    stored_json: str,
) -> None:
    auth_store.set_local_api_token_hash("valid-hash")
    db.execute(
        "UPDATE config_store SET value = %s WHERE key = %s",
        (stored_json, LOCAL_API_TOKEN_HASH_KEY),
    )

    with caplog.at_level(logging.WARNING):
        assert auth_store.get_local_api_token_hash() is None

    assert "Invalid local API token hash" in caplog.text
    assert "gobby auth token --rotate" in caplog.text


def test_local_api_token_hash_write_retries_one_cas_conflict(
    auth_store: AuthStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_store.set_local_api_token_hash("old-hash")
    current_revision = auth_store._config_repository.current_revision
    revisions = iter([0])

    def stale_once() -> int:
        return next(revisions, current_revision())

    monkeypatch.setattr(auth_store._config_repository, "current_revision", stale_once)

    auth_store.set_local_api_token_hash("new-hash")

    assert auth_store.get_local_api_token_hash() == "new-hash"


def test_local_token_helpers_return_bearer_header(local_token_home: None) -> None:
    assert read_local_api_token() is None
    assert daemon_auth_headers() == {}

    local_token_path().write_text("  local-token\n")

    assert read_local_api_token() == "local-token"
    assert daemon_auth_headers() == {"Authorization": "Bearer local-token"}


def test_daemon_auth_headers_prefer_agent_capability(
    local_token_home: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_token_path().write_text("operator-token\n")
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
    local_token_path().write_text("operator-token\n")
    assert daemon_auth_headers() == {"Authorization": "Bearer operator-token"}


def test_ensure_local_api_token_generates(
    auth_store: AuthStore,
    local_token_home: None,
) -> None:
    token = ensure_local_api_token(auth_store)

    assert token is not None
    assert read_local_api_token() == token
    assert auth_store.get_local_api_token_hash() == hash_token(token)
    assert stat.S_IMODE(local_token_path().stat().st_mode) == 0o600


def test_ensure_local_api_token_adopts_existing_file(
    auth_store: AuthStore,
    local_token_home: None,
) -> None:
    local_token_path().write_text("existing-token\n")

    assert ensure_local_api_token(auth_store) == "existing-token"
    assert auth_store.get_local_api_token_hash() == hash_token("existing-token")


def test_ensure_local_api_token_matching_file_and_hash_is_noop(
    auth_store: AuthStore,
    local_token_home: None,
) -> None:
    local_token_path().write_text("matching-token")
    auth_store.set_local_api_token_hash(hash_token("matching-token"))

    assert ensure_local_api_token(auth_store) == "matching-token"
    assert read_local_api_token() == "matching-token"


def test_ensure_local_api_token_hash_only_warns(
    auth_store: AuthStore,
    local_token_home: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    auth_store.set_local_api_token_hash(hash_token("hub-token"))

    with caplog.at_level(logging.WARNING):
        token = ensure_local_api_token(auth_store)

    assert token is None
    assert not local_token_path().exists()
    assert "copy ~/.gobby/local_cli_token from the hub machine" in caplog.text
    assert "gobby auth token --rotate" in caplog.text


def test_ensure_local_api_token_mismatch_warns_and_db_wins(
    auth_store: AuthStore,
    local_token_home: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    local_token_path().write_text("stale-token")
    expected_hash = hash_token("hub-token")
    auth_store.set_local_api_token_hash(expected_hash)

    with caplog.at_level(logging.WARNING):
        token = ensure_local_api_token(auth_store)

    assert token is None
    assert read_local_api_token() == "stale-token"
    assert auth_store.get_local_api_token_hash() == expected_hash
    assert "copy ~/.gobby/local_cli_token from the hub machine" in caplog.text


def test_rotate_local_api_token_replaces_file_and_hash(
    auth_store: AuthStore,
    local_token_home: None,
) -> None:
    old_token = ensure_local_api_token(auth_store)

    new_token = rotate_local_api_token(auth_store)

    assert old_token is not None
    assert new_token != old_token
    assert read_local_api_token() == new_token
    assert auth_store.get_local_api_token_hash() == hash_token(new_token)


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
