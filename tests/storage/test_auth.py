"""Tests for AuthStore session management and secret key detection."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from gobby.storage.auth import AuthStore
from gobby.storage.config_store import is_secret_key_name
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def auth_store(db) -> AuthStore:
    return AuthStore(db)


class TestAuthStoreCreateSession:
    def test_create_session_returns_token_and_expiry(self, auth_store: AuthStore) -> None:
        token, expires_at = auth_store.create_session()
        assert isinstance(token, str)
        assert len(token) == 64  # 32 bytes hex
        assert expires_at is not None

    def test_create_session_stores_only_token_hash(self, db: LocalDatabase) -> None:
        auth_store = AuthStore(db)
        token, _ = auth_store.create_session()

        columns = {row["name"] for row in db.fetchall("PRAGMA table_info(auth_sessions)")}
        row = db.fetchone("SELECT token_hash FROM auth_sessions")

        assert "token" not in columns
        assert row is not None
        assert row["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()

    def test_remember_me_extends_expiry(self, auth_store: AuthStore) -> None:
        _, short_exp = auth_store.create_session(remember_me=False)
        _, long_exp = auth_store.create_session(remember_me=True)
        assert long_exp > short_exp


class TestAuthStoreValidateSession:
    def test_valid_session(self, auth_store: AuthStore) -> None:
        token, _ = auth_store.create_session()
        assert auth_store.validate_session(token) is True

    def test_invalid_token(self, auth_store: AuthStore) -> None:
        assert auth_store.validate_session("nonexistent") is False

    def test_empty_token(self, auth_store: AuthStore) -> None:
        assert auth_store.validate_session("") is False


class TestAuthStoreDeleteSession:
    def test_delete_invalidates(self, auth_store: AuthStore) -> None:
        token, _ = auth_store.create_session()
        assert auth_store.validate_session(token) is True
        auth_store.delete_session(token)
        assert auth_store.validate_session(token) is False


class TestAuthStoreExpiry:
    def test_expired_session_is_invalid(self, db: LocalDatabase) -> None:
        auth_store = AuthStore(db)
        token, _ = auth_store.create_session()
        # Manually expire the session
        db.execute(
            """
            UPDATE auth_sessions
            SET expires_at = '2000-01-01T00:00:00+00:00'
            WHERE token_hash = ?
            """,
            (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
        )
        assert auth_store.validate_session(token) is False


class TestAuthStoreLegacyRepair:
    def test_legacy_sqlite_plaintext_tokens_are_repaired(self, db: LocalDatabase) -> None:
        token = "legacy-token"
        expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        db.execute("DROP TABLE auth_sessions")
        db.execute(
            """
            CREATE TABLE auth_sessions (
                token TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                remember_me INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        db.execute(
            "INSERT INTO auth_sessions (token, expires_at, remember_me) VALUES (?, ?, ?)",
            (token, expires_at, 0),
        )

        run_migrations(db)
        auth_store = AuthStore(db)
        columns = {row["name"] for row in db.fetchall("PRAGMA table_info(auth_sessions)")}
        row = db.fetchone("SELECT token_hash FROM auth_sessions")

        assert "token" not in columns
        assert row is not None
        assert row["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert auth_store.validate_session(token) is True

    def test_constructor_does_not_repair_legacy_sqlite_tokens(self, db: LocalDatabase) -> None:
        db.execute("DROP TABLE auth_sessions")
        db.execute(
            """
            CREATE TABLE auth_sessions (
                token TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                remember_me INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        AuthStore(db)
        columns = {row["name"] for row in db.fetchall("PRAGMA table_info(auth_sessions)")}

        assert "token" in columns
        assert "token_hash" not in columns


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
