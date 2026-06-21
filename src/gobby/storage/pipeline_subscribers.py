"""Completion subscriber persistence methods."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase


class PipelineCompletionSubscriberMixin:
    """Completion subscriber CRUD methods."""

    db: HubDatabase
    project_id: str

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

    def add_completion_subscribers(self, completion_id: str, session_ids: list[str]) -> None:
        """Bulk add subscribers for a completion event.

        Args:
            completion_id: Execution or run ID to subscribe to
            session_ids: Sessions to notify on completion
        """
        if not session_ids:
            return
        self.db.executemany(
            "INSERT INTO completion_subscribers (completion_id, session_id) "
            "VALUES (%s, %s) ON CONFLICT (completion_id, session_id) DO NOTHING",
            [(completion_id, sid) for sid in session_ids],
        )

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

    def remove_completion_subscribers(self, completion_id: str) -> None:
        """Remove all subscribers for a completion event.

        Args:
            completion_id: Execution or run ID
        """
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
        return cursor.rowcount if cursor else 0
