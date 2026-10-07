"""Authentication storage helpers for browser sessions."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import require_stored_datetime, utc_now

# Session durations
SESSION_DURATION = timedelta(hours=12)  # Default (no remember-me)
REMEMBER_ME_DURATION = timedelta(days=30)  # Remember me checked


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    """Manages authentication state in the hub database."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

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

    def session_user_id(self, token: str) -> str | None:
        """Return the user owning an unexpired session, or None."""
        if not token:
            return None
        row = self.db.fetchone(
            "SELECT user_id, expires_at FROM auth_sessions WHERE token_hash = %s",
            (hash_token(token),),
        )
        if not row or utc_now() > require_stored_datetime(row["expires_at"], "expires_at"):
            return None
        return str(row["user_id"])

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
