"""Authentication store for web UI sessions.

Manages auth sessions in the hub database for cookie-based login.
Passwords are encrypted via Fernet in the secrets table (same as API keys).
Sessions are random tokens with expiry.
"""

import hashlib
import logging
import os
from datetime import UTC, datetime, timedelta

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

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
            "INSERT INTO auth_sessions (token_hash, expires_at, remember_me) VALUES (?, ?, ?)",
            (_hash_token(token), expires_at.isoformat(), 1 if remember_me else 0),
        )

        # Opportunistically clean up expired sessions
        self._cleanup_expired()

        return token, expires_at

    def validate_session(self, token: str) -> bool:
        """Check if a session token is valid (exists and not expired)."""
        if not token:
            return False

        row = self.db.fetchone(
            "SELECT expires_at FROM auth_sessions WHERE token_hash = ?",
            (_hash_token(token),),
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
        self.db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_hash_token(token),))
        return True

    def _cleanup_expired(self) -> None:
        """Remove expired sessions."""
        now = datetime.now(UTC).isoformat()
        self.db.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))


def repair_legacy_sqlite_auth_sessions(db: HubDatabase) -> None:
    """Convert legacy SQLite auth_sessions.token rows to token_hash during startup repair."""
    if db.dialect != "sqlite":
        return

    try:
        columns = [row["name"] for row in db.fetchall("PRAGMA table_info(auth_sessions)")]
    except Exception:
        logger.debug("Auth session repair skipped; auth_sessions table unavailable")
        return

    if not columns or "token_hash" in columns or "token" not in columns:
        return

    rows = db.fetchall("SELECT token, created_at, expires_at, remember_me FROM auth_sessions")
    with db.transaction() as conn:
        conn.execute("DROP TABLE IF EXISTS auth_sessions_new")
        conn.execute(
            """
            CREATE TABLE auth_sessions_new (
                token_hash TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                remember_me INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        for row in rows:
            token = row["token"]
            if not token:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO auth_sessions_new (
                    token_hash, created_at, expires_at, remember_me
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    _hash_token(str(token)),
                    row["created_at"],
                    row["expires_at"],
                    row["remember_me"],
                ),
            )
        conn.execute("DROP TABLE auth_sessions")
        conn.execute("ALTER TABLE auth_sessions_new RENAME TO auth_sessions")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at)"
        )
