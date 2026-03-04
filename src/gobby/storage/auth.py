"""Authentication store for web UI sessions.

Manages auth sessions in SQLite for cookie-based login.
Passwords are encrypted via Fernet in the secrets table (same as API keys).
Sessions are random tokens with expiry.
"""

import logging
import os
from datetime import UTC, datetime, timedelta

from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

# Session durations
SESSION_DURATION = timedelta(hours=12)  # Default (no remember-me)
REMEMBER_ME_DURATION = timedelta(days=30)  # Remember me checked


class AuthStore:
    """Manages auth sessions in SQLite."""

    def __init__(self, db: DatabaseProtocol) -> None:
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
            "INSERT INTO auth_sessions (token, expires_at, remember_me) VALUES (?, ?, ?)",
            (token, expires_at.isoformat(), 1 if remember_me else 0),
        )

        # Opportunistically clean up expired sessions
        self._cleanup_expired()

        return token, expires_at

    def validate_session(self, token: str) -> bool:
        """Check if a session token is valid (exists and not expired)."""
        if not token:
            return False

        row = self.db.fetchone(
            "SELECT expires_at FROM auth_sessions WHERE token = ?",
            (token,),
        )
        if not row:
            return False

        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        if datetime.now(UTC) > expires_at:
            self.delete_session(token)
            return False

        return True

    def delete_session(self, token: str) -> bool:
        """Delete a session (logout)."""
        self.db.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
        return True

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = datetime.now(UTC).isoformat()
        self.db.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
