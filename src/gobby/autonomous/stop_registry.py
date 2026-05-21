"""Stop signal registry for autonomous session management.

Provides thread-safe stop signal management for autonomous workflows.
External systems (HTTP, WebSocket, CLI, MCP) can signal sessions to stop
gracefully, and workflows can check for pending stop signals at step
transitions.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase

logger = logging.getLogger(__name__)


@dataclass
class StopSignal:
    """Represents a stop signal for a session."""

    session_id: str
    source: str  # http, websocket, cli, mcp, workflow
    reason: str | None
    requested_at: datetime
    acknowledged_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        """Return True if signal has not been acknowledged."""
        return self.acknowledged_at is None


class StopRegistry:
    """Thread-safe registry for session stop signals.

    Stop signals can be sent from multiple sources:
    - HTTP endpoint: POST /api/v1/sessions/{session_id}/stop
    - WebSocket: stop_session message
    - CLI: gobby session stop <session_id>
    - MCP: gobby-sessions.request_stop tool
    - Workflow: check_stop_signal action detecting stuck state

    Workflows check for stop signals via the check_stop_signal action
    or the has_stop_signal() condition function.
    """

    def __init__(self, db: "LocalDatabase"):
        """Initialize the stop registry.

        Args:
            db: Database connection for persistent storage
        """
        self.db = db
        self._lock = threading.Lock()

    def signal_stop(
        self,
        session_id: str,
        source: str = "unknown",
        reason: str | None = None,
    ) -> StopSignal:
        """Request a session to stop.

        Args:
            session_id: The session to signal
            source: Source of the stop request (http, websocket, cli, mcp, workflow)
            reason: Optional reason for the stop request

        Returns:
            The created StopSignal
        """
        now = datetime.now(UTC)

        with self._lock:
            # Check if there's already a pending signal
            existing = self.get_signal(session_id)
            if existing and existing.is_pending:
                logger.debug(
                    f"Stop signal already pending for session {session_id} from {existing.source}"
                )
                return existing

            # Insert new signal
            self.db.execute(
                """
                INSERT INTO session_stop_signals (session_id, source, reason, requested_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    source = excluded.source,
                    reason = excluded.reason,
                    requested_at = excluded.requested_at,
                    acknowledged_at = NULL
                """,
                (session_id, source, reason, now.isoformat()),
            )

            logger.info(
                f"Stop signal sent for session {session_id} from {source}: {reason or 'no reason'}"
            )

            return StopSignal(
                session_id=session_id,
                source=source,
                reason=reason,
                requested_at=now,
            )

    def get_signal(self, session_id: str) -> StopSignal | None:
        """Get the stop signal for a session if one exists.

        Args:
            session_id: The session to check

        Returns:
            StopSignal if one exists, None otherwise
        """
        row = self.db.fetchone(
            """
            SELECT session_id, source, reason, requested_at, acknowledged_at
            FROM session_stop_signals
            WHERE session_id = ?
            """,
            (session_id,),
        )

        if not row:
            return None

        return StopSignal(
            session_id=row["session_id"],
            source=row["source"],
            reason=row["reason"],
            requested_at=datetime.fromisoformat(row["requested_at"]),
            acknowledged_at=(
                datetime.fromisoformat(row["acknowledged_at"]) if row["acknowledged_at"] else None
            ),
        )

    def has_pending_signal(self, session_id: str) -> bool:
        """Check if a session has a pending stop signal.

        Args:
            session_id: The session to check

        Returns:
            True if there is an unacknowledged stop signal
        """
        signal = self.get_signal(session_id)
        return signal is not None and signal.is_pending

    def acknowledge(self, session_id: str) -> bool:
        """Acknowledge a stop signal (session is stopping).

        Args:
            session_id: The session acknowledging the stop

        Returns:
            True if a signal was acknowledged, False if none existed
        """
        now = datetime.now(UTC)

        with self._lock:
            result = self.db.execute(
                """
                UPDATE session_stop_signals
                SET acknowledged_at = ?
                WHERE session_id = ? AND acknowledged_at IS NULL
                """,
                (now.isoformat(), session_id),
            )

            if result.rowcount > 0:
                logger.info(f"Stop signal acknowledged for session {session_id}")
                return True
            return False

    def clear(self, session_id: str) -> bool:
        """Clear any stop signal for a session.

        Use this when a session has fully stopped and we want to clean up.

        Args:
            session_id: The session to clear

        Returns:
            True if a signal was cleared, False if none existed
        """
        with self._lock:
            result = self.db.execute(
                "DELETE FROM session_stop_signals WHERE session_id = ?",
                (session_id,),
            )

            if result.rowcount > 0:
                logger.debug(f"Stop signal cleared for session {session_id}")
                return True
            return False

    def list_pending(self, project_id: str | None = None) -> list[StopSignal]:
        """List all pending stop signals.

        Args:
            project_id: Optional project filter (requires join with sessions)

        Returns:
            List of pending StopSignals
        """
        if project_id:
            rows = self.db.fetchall(
                """
                SELECT ss.session_id, ss.source, ss.reason, ss.requested_at, ss.acknowledged_at
                FROM session_stop_signals ss
                JOIN sessions s ON ss.session_id = s.id
                WHERE ss.acknowledged_at IS NULL AND s.project_id = ?
                ORDER BY ss.requested_at DESC
                """,
                (project_id,),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT session_id, source, reason, requested_at, acknowledged_at
                FROM session_stop_signals
                WHERE acknowledged_at IS NULL
                ORDER BY requested_at DESC
                """,
            )

        return [
            StopSignal(
                session_id=row["session_id"],
                source=row["source"],
                reason=row["reason"],
                requested_at=datetime.fromisoformat(row["requested_at"]),
                acknowledged_at=None,
            )
            for row in rows
        ]

    def cleanup_stale(self, max_age_hours: int = 24) -> int:
        """Clean up old acknowledged signals.

        Args:
            max_age_hours: Remove acknowledged signals older than this

        Returns:
            Number of signals cleaned up
        """
        from datetime import timedelta

        threshold = datetime.now(UTC) - timedelta(hours=max_age_hours)

        with self._lock:
            result = self.db.execute(
                """
                DELETE FROM session_stop_signals
                WHERE acknowledged_at IS NOT NULL
                AND acknowledged_at < ?
                """,
                (threshold.isoformat(),),
            )

            if result.rowcount > 0:
                logger.info(f"Cleaned up {result.rowcount} stale stop signal(s)")
            return result.rowcount
