"""Authentication store for web UI sessions.

Manages auth sessions in the hub database for cookie-based login.
Passwords are encrypted via Fernet in the secrets table (same as API keys).
Sessions are random tokens with expiry.
"""

import hashlib
import os
from datetime import UTC, datetime, timedelta

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import require_stored_datetime, utc_now

# Session durations
SESSION_DURATION = timedelta(hours=12)  # Default (no remember-me)
REMEMBER_ME_DURATION = timedelta(days=30)  # Remember me checked


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
            (_hash_token(token), expires_at, bool(remember_me)),
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
            (_hash_token(token),),
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
        self.db.execute("DELETE FROM auth_sessions WHERE token_hash = %s", (_hash_token(token),))
        return True

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = utc_now()
        self.db.execute("DELETE FROM auth_sessions WHERE expires_at < %s", (now,))
