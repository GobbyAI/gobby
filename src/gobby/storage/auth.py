"""Authentication storage helpers for daemon tokens, passwords, and sessions."""

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import write_private_file
from gobby.utils.datetime import require_stored_datetime, utc_now
from gobby.utils.local_token import local_token_path, read_local_api_token

logger = logging.getLogger(__name__)

# Session durations
SESSION_DURATION = timedelta(hours=12)  # Default (no remember-me)
REMEMBER_ME_DURATION = timedelta(days=30)  # Remember me checked
LOCAL_API_TOKEN_HASH_KEY = "auth.api_token_hash"
PASSWORD_HASH_KEY = "auth.password_hash"
USERNAME_KEY = "auth.username"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_INVALID_PASSWORD_DIGEST = bytes([0xFF]) * _SCRYPT_DKLEN
_EMPTY_PASSWORD_DIGEST = bytes(_SCRYPT_DKLEN)
_TOKEN_REMEDIATION = (
    "copy ~/.gobby/local_cli_token from the hub machine or run 'gobby auth token --rotate'"
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Create the canonical scrypt password hash stored in config_store."""
    password_salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=password_salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(password_salt).decode("ascii"),
            base64.b64encode(derived).decode("ascii"),
        )
    )


def verify_password_hash(password: str, stored_hash: str | None) -> bool:
    """Verify a password against the canonical scrypt representation."""
    expected = _INVALID_PASSWORD_DIGEST
    derived = _EMPTY_PASSWORD_DIGEST
    valid_format = False

    if stored_hash is not None:
        parts = stored_hash.split("$")
        expected_prefix = ["scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P)]
        if len(parts) == 6 and parts[:4] == expected_prefix:
            try:
                salt = base64.b64decode(parts[4], validate=True)
                candidate = base64.b64decode(parts[5], validate=True)
            except (ValueError, binascii.Error):
                pass
            else:
                if salt and len(candidate) == _SCRYPT_DKLEN:
                    expected = candidate
                    derived = hashlib.scrypt(
                        password.encode("utf-8"),
                        salt=salt,
                        n=_SCRYPT_N,
                        r=_SCRYPT_R,
                        p=_SCRYPT_P,
                        dklen=_SCRYPT_DKLEN,
                    )
                    valid_format = True

    return valid_format and hmac.compare_digest(derived, expected)


def _write_new_local_api_token() -> str:
    token = secrets.token_urlsafe(32)
    write_private_file(local_token_path(), token.encode("utf-8"))
    return token


def ensure_local_api_token(config_store: ConfigStore) -> str | None:
    """Reconcile the local token file with the authoritative stored hash."""
    token = read_local_api_token()
    stored_hash = config_store.get(LOCAL_API_TOKEN_HASH_KEY)

    if stored_hash is not None and not isinstance(stored_hash, str):
        logger.warning("Invalid local API token hash in config store; %s", _TOKEN_REMEDIATION)
        return None

    if token is not None and stored_hash:
        if hash_token(token) == stored_hash:
            return token
        logger.warning("Local API token file does not match the hub; %s", _TOKEN_REMEDIATION)
        return None

    if token is not None:
        config_store.set(LOCAL_API_TOKEN_HASH_KEY, hash_token(token), source="system")
        return token

    if stored_hash:
        logger.warning("Local API token file is missing; %s", _TOKEN_REMEDIATION)
        return None

    token = _write_new_local_api_token()
    config_store.set(LOCAL_API_TOKEN_HASH_KEY, hash_token(token), source="system")
    return token


def rotate_local_api_token(config_store: ConfigStore) -> str:
    """Replace the local API token and its authoritative stored hash."""
    token = _write_new_local_api_token()
    config_store.set(LOCAL_API_TOKEN_HASH_KEY, hash_token(token), source="system")
    return token


class AuthStore:
    """Manages auth sessions in the hub database."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def create_session(self, remember_me: bool = False) -> tuple[str, datetime]:
        """Create a new auth session.

        Returns:
            Tuple of (token, expires_at)
        """
        token = os.urandom(32).hex()
        duration = REMEMBER_ME_DURATION if remember_me else SESSION_DURATION
        expires_at = datetime.now(UTC) + duration

        self.db.execute(
            "INSERT INTO auth_sessions (token_hash, expires_at, remember_me) VALUES (%s, %s, %s)",
            (hash_token(token), expires_at, bool(remember_me)),
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
