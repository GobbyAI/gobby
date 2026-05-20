"""Terminal metadata mixin for session storage."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.session_models import Session

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...


class _TerminalMixin:
    def get_sessions_since(
        self: _ManagerState, since: datetime, project_id: str | None = None
    ) -> list[Session]:
        """
        Get sessions created since a given timestamp.

        Used for aggregating usage over a time period.

        Args:
            since: Datetime to query from (sessions created after this time)
            project_id: Optional project ID to filter by

        Returns:
            List of sessions created since the given timestamp
        """
        since_str = since.isoformat()

        if project_id:
            rows = self.db.fetchall(
                """
                SELECT * FROM sessions
                WHERE created_at >= ?
                AND project_id = ?
                ORDER BY created_at DESC
                """,
                (since_str, project_id),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM sessions
                WHERE created_at >= ?
                ORDER BY created_at DESC
                """,
                (since_str,),
            )

        return [Session.from_row(row) for row in rows]

    def update_terminal_pickup_metadata(
        self: _ManagerState,
        session_id: str,
        workflow_name: str | None = None,
        agent_run_id: str | None = None,
        context_injected: bool | None = None,
        original_prompt: str | None = None,
    ) -> Session | None:
        """
        Update terminal pickup metadata for a session.

        These fields are used when a terminal-mode agent picks up its
        prepared state via hooks on session start.

        Args:
            session_id: Session ID to update.
            workflow_name: Workflow to activate on terminal pickup.
            agent_run_id: Link back to the agent run record.
            context_injected: Whether context was injected into prompt.
            original_prompt: Original prompt for the agent.

        Returns:
            Updated session or None if not found.
        """
        values: dict[str, Any] = {}

        if workflow_name is not None:
            values["workflow_name"] = workflow_name
        if agent_run_id is not None:
            values["agent_run_id"] = agent_run_id
        if context_injected is not None:
            values["context_injected"] = 1 if context_injected else 0
        if original_prompt is not None:
            values["original_prompt"] = original_prompt

        if not values:
            return self.get(session_id)

        values["updated_at"] = datetime.now(UTC).isoformat()

        self.db.safe_update("sessions", values, "id = ?", (session_id,))
        return self.get(session_id)

    def record_skills_used(self: _ManagerState, session_id: str, skill_names: list[str]) -> int:
        """Record skills used in a session (idempotent via UNIQUE constraint).

        Args:
            session_id: Session ID
            skill_names: List of skill names that were injected

        Returns:
            Number of new skills recorded
        """
        now = datetime.now(UTC).isoformat()
        count = 0
        with self.db.transaction():
            for name in skill_names:
                cursor = self.db.execute(
                    "INSERT INTO session_skills (session_id, skill_name, created_at) "
                    "VALUES (?, ?, ?) ON CONFLICT (session_id, skill_name) DO NOTHING",
                    (session_id, name, now),
                )
                if cursor.rowcount == 1:
                    count += 1
        return count
