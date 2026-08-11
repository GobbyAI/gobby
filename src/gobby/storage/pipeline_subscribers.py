"""Completion subscriber persistence methods."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase


class PipelineSubscriberStorageError(RuntimeError):
    """Transport-neutral failure while querying completion subscribers."""


class PipelineCompletionSubscriberMixin:
    """Completion subscriber CRUD methods."""

    db: HubDatabase
    project_id: str | None

    def add_completion_subscriber(self, completion_id: str, session_id: str) -> None:
        """Add a subscriber for a completion event (idempotent).

        Args:
            completion_id: Execution or run ID to subscribe to
            session_id: Session to notify on completion
        """
        self.db.execute(
            """
            INSERT INTO completion_subscribers (completion_id, session_id)
            VALUES (%s, %s)
            ON CONFLICT (completion_id, session_id) DO NOTHING
            """,
            (completion_id, session_id),
        )

    def add_completion_subscribers(self, completion_id: str, session_ids: list[str]) -> list[str]:
        """Bulk add subscribers for a completion event.

        Args:
            completion_id: Execution or run ID to subscribe to
            session_ids: Sessions to notify on completion

        Returns:
            Session IDs whose durable rows were created by this call.
        """
        unique_session_ids = list(dict.fromkeys(session_ids))
        if not unique_session_ids:
            return []

        values = ", ".join("(%s, %s)" for _ in unique_session_ids)
        params = tuple(
            value for session_id in unique_session_ids for value in (completion_id, session_id)
        )
        rows = self.db.execute(
            f"""
            INSERT INTO completion_subscribers (completion_id, session_id)
            VALUES {values}
            ON CONFLICT (completion_id, session_id) DO NOTHING
            RETURNING session_id
            """,  # nosec B608
            params,
        ).fetchall()
        inserted = {str(row["session_id"]) for row in rows}
        return [session_id for session_id in unique_session_ids if session_id in inserted]

    def get_completion_subscribers(self, completion_id: str) -> list[str]:
        """Get all subscriber session IDs for a completion event.

        Args:
            completion_id: Execution or run ID

        Returns:
            List of subscribed session IDs
        """
        rows = self.db.fetchall(
            "SELECT session_id FROM completion_subscribers WHERE completion_id = %s",
            (completion_id,),
        )
        return [row["session_id"] for row in rows]

    def list_completion_ids(self) -> list[str]:
        """List completion IDs that still have durable subscribers."""
        rows = self.db.fetchall(
            "SELECT DISTINCT completion_id FROM completion_subscribers ORDER BY completion_id"
        )
        return [str(row["completion_id"]) for row in rows]

    def has_active_agent_wait(self, session_id: str) -> bool:
        """Return whether a session is durably waiting on one of its active agent runs."""
        from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES

        try:
            row = self.db.fetchone(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM completion_subscribers AS subscribers
                    JOIN agent_runs AS runs ON runs.id = subscribers.completion_id
                    WHERE subscribers.session_id = %s
                      AND runs.parent_session_id = %s
                      AND runs.status = ANY(%s)
                ) AS has_active_agent_wait
                """,
                (session_id, session_id, list(ACTIVE_AGENT_RUN_STATUSES)),
            )
        except Exception as exc:
            raise PipelineSubscriberStorageError(
                f"Failed to query active agent wait for session {session_id}"
            ) from exc
        return bool(row and row["has_active_agent_wait"])

    def remove_completion_subscribers(
        self,
        completion_id: str,
        *,
        session_ids: list[str] | None = None,
    ) -> None:
        """Remove all or selected subscribers for a completion event.

        Args:
            completion_id: Execution or run ID
            session_ids: Optional sessions to remove; ``None`` removes all
        """
        if session_ids is not None:
            if not session_ids:
                return
            self.db.execute(
                "DELETE FROM completion_subscribers "
                "WHERE completion_id = %s AND session_id = ANY(%s)",
                (completion_id, session_ids),
            )
            return

        self.db.execute(
            "DELETE FROM completion_subscribers WHERE completion_id = %s",
            (completion_id,),
        )

    def remove_completion_subscribers_for_terminal_agent_runs(self) -> int:
        """Remove subscriber rows whose completion ID is a terminal agent run."""
        from gobby.storage.agents import TERMINAL_AGENT_RUN_STATUSES

        status_placeholders = ", ".join("%s" for _ in TERMINAL_AGENT_RUN_STATUSES)
        cursor = self.db.execute(
            f"""
            DELETE FROM completion_subscribers
            WHERE completion_id IN (
                SELECT id
                FROM agent_runs
                WHERE status IN ({status_placeholders})
            )
        """,  # nosec B608
            TERMINAL_AGENT_RUN_STATUSES,
        )
        return max(cursor.rowcount, 0) if cursor else 0


class CompletionSubscriberManager(PipelineCompletionSubscriberMixin):
    """Projectless manager for completion subscriber CRUD."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db
        self.project_id = None
