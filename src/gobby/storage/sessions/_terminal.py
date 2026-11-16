"""Terminal metadata mixin for session storage."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.session_models import Session
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.utils.datetime import to_aware_utc, utc_now
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def _notify_status_transition(self, transition: SessionStatusTransition) -> None: ...


class _TerminalMixin:
    def rebind_resumed_terminal_session(
        self: _ManagerState,
        session_id: str,
        *,
        machine_id: str,
        project_id: str,
        source: str,
        transcript_path: str | None,
        terminal_context: dict[str, Any] | None,
        workflow_name: str | None,
        agent_depth: int,
        sandbox_enabled: bool | None,
    ) -> Session | None:
        """Explicitly rebind a persisted terminal row to a freshly observed runtime."""
        current = self.get(session_id)
        if current is None or current.session_type != "terminal" or current.status == "deleted":
            return None
        if current.machine_id != machine_id:
            raise MachineOwnershipMismatchError(
                resource_kind="session",
                resource_id=current.id,
                owner_machine_id=current.machine_id,
                current_machine_id=machine_id,
            )

        now = utc_now()
        context_json = json.dumps(
            {key: value for key, value in (terminal_context or {}).items() if value is not None}
        )
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE sessions
                SET project_id = %s,
                    source = %s,
                    transcript_path = %s,
                    terminal_context = %s::jsonb,
                    workflow_name = COALESCE(%s, workflow_name),
                    agent_depth = %s,
                    sandbox_enabled = COALESCE(%s, sandbox_enabled),
                    status = 'active',
                    transcript_processed = FALSE,
                    updated_at = %s,
                    last_activity = %s
                WHERE id = %s
                  AND session_type = 'terminal'
                  AND status != 'deleted'
                """,
                (
                    project_id,
                    source,
                    transcript_path,
                    context_json,
                    workflow_name,
                    agent_depth,
                    sandbox_enabled,
                    now,
                    now,
                    session_id,
                ),
            )
            if cursor.rowcount <= 0:
                return None

        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
            if current.status != "active":
                self._notify_status_transition(
                    SessionStatusTransition.from_session(updated, transitioned_at=now)
                )
        return updated

    def continue_terminal_session_as_web_chat(
        self: _ManagerState,
        session_id: str,
        *,
        source: str,
        model: str | None,
        project_id: str,
        sandbox_policy_hash: str | None,
    ) -> Session | None:
        """Explicitly convert one terminal row into its in-place web continuation."""
        current = self.get(session_id)
        if current is None or current.session_type != "terminal" or current.status == "deleted":
            return None
        machine_id = require_machine_id()
        if current.machine_id != machine_id:
            raise MachineOwnershipMismatchError(
                resource_kind="session",
                resource_id=current.id,
                owner_machine_id=current.machine_id,
                current_machine_id=machine_id,
            )

        now = utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE sessions
                SET source = %s,
                    model = COALESCE(%s, model),
                    project_id = %s,
                    session_type = 'web_chat',
                    status = 'active',
                    terminal_context = '{}'::jsonb,
                    sandbox_enabled = FALSE,
                    sandbox_policy_hash = %s,
                    updated_at = %s,
                    last_activity = %s
                WHERE id = %s
                  AND machine_id = %s
                  AND session_type = 'terminal'
                  AND status != 'deleted'
                """,
                (
                    source,
                    model,
                    project_id,
                    sandbox_policy_hash,
                    now,
                    now,
                    session_id,
                    machine_id,
                ),
            )
            if cursor.rowcount <= 0:
                return None

        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
            if current.status != "active":
                self._notify_status_transition(
                    SessionStatusTransition.from_session(updated, transitioned_at=now)
                )
        return updated

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
        since_value = to_aware_utc(since)

        if project_id:
            rows = self.db.fetchall(
                """
                SELECT * FROM sessions LEFT JOIN (SELECT id AS project_id, name AS project_name FROM projects) AS session_projects USING (project_id)
                WHERE created_at >= %s
                AND project_id = %s
                ORDER BY created_at DESC
                """,
                (since_value, project_id),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM sessions LEFT JOIN (SELECT id AS project_id, name AS project_name FROM projects) AS session_projects USING (project_id)
                WHERE created_at >= %s
                ORDER BY created_at DESC
                """,
                (since_value,),
            )

        return [Session.from_row(row) for row in rows]

    def update_terminal_pickup_metadata(
        self: _ManagerState,
        session_id: str,
        workflow_name: str | None = None,
        agent_run_id: str | None = None,
        original_prompt: str | None = None,
    ) -> Session | None:
        """
        Update terminal pickup metadata for a session.

        These fields are used when a terminal-mode agent picks up its
        prepared state via hooks on session start. ``context_injected`` is
        not settable here: it is derived by the startup-context claim commit.

        Args:
            session_id: Session ID to update.
            workflow_name: Workflow to activate on terminal pickup.
            agent_run_id: Link back to the agent run record.
            original_prompt: Original prompt for the agent.

        Returns:
            Updated session or None if not found.
        """
        values: dict[str, Any] = {}

        if workflow_name is not None:
            values["workflow_name"] = workflow_name
        if agent_run_id is not None:
            values["agent_run_id"] = agent_run_id
        if original_prompt is not None:
            values["original_prompt"] = original_prompt

        if not values:
            return self.get(session_id)

        values["updated_at"] = utc_now()

        self.db.safe_update("sessions", values, "id = %s", (session_id,))
        return self.get(session_id)

    def record_skills_used(self: _ManagerState, session_id: str, skill_names: list[str]) -> int:
        """Record skills used in a session (idempotent via UNIQUE constraint).

        Args:
            session_id: Session ID
            skill_names: List of skill names that were injected

        Returns:
            Number of new skills recorded
        """
        count = 0
        with self.db.transaction():
            for name in skill_names:
                cursor = self.db.execute(
                    "INSERT INTO session_skills (session_id, skill_name) "
                    "VALUES (%s, %s) ON CONFLICT (session_id, skill_name) DO NOTHING",
                    (session_id, name),
                )
                if cursor.rowcount == 1:
                    count += 1
        return count
