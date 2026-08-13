"""Authentication storage helpers for daemon tokens and sessions."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from gobby.storage.config_mutations import ConfigConflictError, ConfigMutations, ConfigPatch
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub._ambient import ambient_transaction
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import write_private_file
from gobby.utils.datetime import require_stored_datetime, utc_now
from gobby.utils.local_token import local_token_path, read_local_api_token

logger = logging.getLogger(__name__)

# Session durations
SESSION_DURATION = timedelta(hours=12)  # Default (no remember-me)
REMEMBER_ME_DURATION = timedelta(days=30)  # Remember me checked
LOCAL_API_TOKEN_HASH_KEY = "auth.api_token_hash"
_TOKEN_REMEDIATION = (
    "copy ~/.gobby/local_cli_token from the hub machine or run 'gobby auth token --rotate'"
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_new_local_api_token() -> str:
    token = secrets.token_urlsafe(32)
    write_private_file(local_token_path(), token.encode("utf-8"))
    return token


def ensure_local_api_token(auth_store: AuthStore) -> str | None:
    """Reconcile the local token file with the authoritative stored hash."""
    token = read_local_api_token()
    stored_hash, invalid = auth_store._read_local_api_token_hash()

    if invalid:
        return None

    if token is not None and stored_hash:
        if hash_token(token) == stored_hash:
            return token
        logger.warning("Local API token file does not match the hub; %s", _TOKEN_REMEDIATION)
        return None

    if token is not None:
        auth_store.set_local_api_token_hash(hash_token(token))
        return token

    if stored_hash:
        logger.warning("Local API token file is missing; %s", _TOKEN_REMEDIATION)
        return None

    token = _write_new_local_api_token()
    auth_store.set_local_api_token_hash(hash_token(token))
    return token


def rotate_local_api_token(auth_store: AuthStore) -> str:
    """Replace the local API token and its authoritative stored hash."""
    token = _write_new_local_api_token()
    auth_store.set_local_api_token_hash(hash_token(token))
    return token


class AuthStore:
    """Manages authentication state in the hub database."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db
        self._config_mutations: ConfigMutations | None = None
        self._config_repository = ConfigRepository(db)

    def get_local_api_token_hash(self) -> str | None:
        """Return the stored local API token hash when it is usable."""
        token_hash, _invalid = self._read_local_api_token_hash()
        return token_hash

    def _read_local_api_token_hash(self) -> tuple[str | None, bool]:
        snapshot = self._config_repository.read(resolve_secrets=False)
        token_hash = snapshot.overrides.get(LOCAL_API_TOKEN_HASH_KEY)
        if token_hash is None:
            return None, False
        if not isinstance(token_hash, str) or not token_hash:
            logger.warning("Invalid local API token hash in config store; %s", _TOKEN_REMEDIATION)
            return None, True
        return token_hash, False

    def set_local_api_token_hash(self, token_hash: str) -> None:
        """Store the local API token hash with one bounded CAS retry."""
        mutations = self._config_mutations
        if mutations is None:
            mutations = ConfigMutations(self.db)
            self._config_mutations = mutations
        attempts = 2 if ambient_transaction(self.db) is None else 1
        patch = ConfigPatch(values={LOCAL_API_TOKEN_HASH_KEY: token_hash})
        for attempt in range(attempts):
            revision = self._config_repository.current_revision()
            try:
                mutations.patch_internal(
                    expected_revision=revision,
                    patch=patch,
                    source="system",
                )
                return
            except ConfigConflictError:
                if attempt + 1 >= attempts:
                    raise
        raise AssertionError("unreachable")

    def create_session(self, user_id: str, remember_me: bool = False) -> tuple[str, datetime]:
        """Create a new auth session.

        Returns:
            Tuple of (token, expires_at)
        """
        token = os.urandom(32).hex()
        session_id = str(uuid.uuid4())
        normalized_user_id = str(uuid.UUID(user_id.strip()))
        duration = REMEMBER_ME_DURATION if remember_me else SESSION_DURATION
        expires_at = datetime.now(UTC) + duration

        self.db.execute(
            """
            INSERT INTO auth_sessions (id, user_id, token_hash, expires_at, remember_me)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (session_id, normalized_user_id, hash_token(token), expires_at, bool(remember_me)),
        )

        # Opportunistically clean up expired sessions
        self._cleanup_expired()

        return token, expires_at

    def validate_session(self, token: str) -> bool:
        """Check if a session token is valid (exists and not expired)."""
        if not token:
            return False

        row = self.db.fetchone(
            "SELECT expires_at FROM auth_sessions WHERE token_hash = %s",
            (hash_token(token),),
        )
        if not row:
            return False

        expires_at = require_stored_datetime(row["expires_at"], "expires_at")
        if utc_now() > expires_at:
            self.delete_session(token)
            return False

        return True

    def delete_session(self, token: str) -> bool:
        """Delete a session (logout)."""
        cursor = self.db.execute(
            "DELETE FROM auth_sessions WHERE token_hash = %s", (hash_token(token),)
        )
        return cursor.rowcount > 0

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = utc_now()
        self.db.execute("DELETE FROM auth_sessions WHERE expires_at < %s", (now,))
