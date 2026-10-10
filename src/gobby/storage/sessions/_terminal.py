"""Terminal metadata mixin for session storage."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from gobby.sessions.contested_expiry import ContestedExpiryCause
from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.session_models import Session
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.utils.datetime import to_aware_utc, utc_now
from gobby.utils.machine_id import require_machine_id

from ._constants import LIVE_SESSION_STATUS_ORDER, LIVE_SESSION_STATUSES

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...

    def update_parent_session_id(
        self, session_id: str, parent_session_id: str | None
    ) -> Session | None: ...

    def mark_session_expired(self, session_id: str, *, cause: ContestedExpiryCause) -> bool: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def _notify_status_transition(self, transition: SessionStatusTransition) -> None: ...

    def _detach_tmux_sessions(
        self,
        machine_id: str,
        socket_identity: str,
        *,
        pane: str | None = None,
    ) -> list[str]: ...


_TMUX_CONTEXT_KEYS = (
    "tmux_pane",
    "tmux_window_id",
    "tmux_session",
    "tmux_socket_path",
    "tmux_socket_name",
    "tmux_socket",
)

_TMUX_SOCKET_IDENTITY_SQL = """
CASE
    WHEN NULLIF(BTRIM(terminal_context ->> 'tmux_socket_path'), '') IS NOT NULL
        THEN 'tmux_socket_path:' || BTRIM(terminal_context ->> 'tmux_socket_path')
    WHEN NULLIF(BTRIM(terminal_context ->> 'tmux_socket_name'), '') IS NOT NULL
        THEN 'tmux_socket_name:' || BTRIM(terminal_context ->> 'tmux_socket_name')
    WHEN NULLIF(BTRIM(terminal_context ->> 'tmux_socket'), '') IS NOT NULL
        THEN 'tmux_socket:' || BTRIM(terminal_context ->> 'tmux_socket')
    ELSE NULL
END
"""


class _TerminalMixin:
    def bind_native_clear_successor(self: _ManagerState, successor_id: str) -> Session | None:
        """Link a native clear to its terminal predecessor and notify routing.

        SessionEnd may have expired the predecessor before SessionStart arrives.
        Publish after the link even when expiry itself is no longer a transition.
        Staged clears retain their marker-driven binding and routing contract.
        """
        from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
        from gobby.sessions.handoff_identity import terminal_contexts_match_for_replacement

        from ._contested_expiry import read_session_variables

        successor = self.get(successor_id)
        if (
            successor is None
            or successor.session_type != "terminal"
            or successor.status not in LIVE_SESSION_STATUSES
            or successor.parent_session_id is not None
            or not successor.terminal_context
        ):
            return None
        rows = self.db.fetchall(
            """
            SELECT * FROM sessions
            WHERE project_id = %s AND machine_id = %s AND source = %s
              AND session_type = 'terminal' AND agent_depth = %s
              AND status IN ('active', 'paused', 'expired')
              AND terminal_context IS NOT NULL AND id <> %s
              AND created_at <= %s
              AND (terminal_context ->> 'tmux_pane' = %s
                   OR terminal_context ->> 'tty' = %s)
            ORDER BY created_at DESC, id DESC
            LIMIT 250
            """,
            (
                successor.project_id,
                successor.machine_id,
                successor.source,
                successor.agent_depth,
                successor.id,
                successor.created_at,
                *(
                    str(successor.terminal_context[key])
                    if successor.terminal_context.get(key)
                    else None
                    for key in ("tmux_pane", "tty")
                ),
            ),
        )
        for row in rows:
            predecessor = Session.from_row(row)
            if not terminal_contexts_match_for_replacement(
                successor.terminal_context, predecessor.terminal_context
            ):
                continue
            variables = read_session_variables(
                self.db, predecessor.id, keys=(CLEAR_ATTEMPT_VARIABLE,)
            )
            if isinstance((variables or {}).get(CLEAR_ATTEMPT_VARIABLE), dict):
                return None
            self.update_parent_session_id(successor.id, predecessor.id)
            if not self.mark_session_expired(predecessor.id, cause="parent_registration"):
                return None
            ended = self.get(predecessor.id)
            if ended is not None:
                self._notify_status_transition(SessionStatusTransition.from_session(ended))
            return ended
        return None

    def expire_tmux_socket_sessions(
        self: _ManagerState,
        machine_id: str,
        socket_identity: str,
    ) -> list[str]:
        """Expire and detach every session referencing one missing tmux server."""
        return self._detach_tmux_sessions(machine_id, socket_identity)

    def expire_tmux_pane_sessions(
        self: _ManagerState,
        machine_id: str,
        socket_identity: str,
        pane: str,
    ) -> list[str]:
        """Expire and detach every session referencing one missing tmux pane."""
        return self._detach_tmux_sessions(machine_id, socket_identity, pane=pane)

    def _detach_tmux_sessions(
        self: _ManagerState,
        machine_id: str,
        socket_identity: str,
        *,
        pane: str | None = None,
    ) -> list[str]:
        now = utc_now()
        pane_clause = "" if pane is None else "AND BTRIM(terminal_context ->> 'tmux_pane') = %s"
        params: list[Any] = [machine_id, socket_identity]
        if pane is not None:
            params.append(pane)

        with self.db.transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM sessions LEFT JOIN (SELECT id AS project_id, name AS project_name FROM projects) AS session_projects USING (project_id)
                WHERE machine_id IS NOT DISTINCT FROM %s
                  AND {_TMUX_SOCKET_IDENTITY_SQL} = %s
                  {pane_clause}
                FOR UPDATE OF sessions
                """,
                tuple(params),
            ).fetchall()
            sessions = [Session.from_row(row) for row in rows]
            if not sessions:
                return []

            session_ids = [session.id for session in sessions]
            conn.execute(
                """
                UPDATE sessions
                SET status = CASE
                        WHEN status = ANY(%s) THEN 'expired'
                        ELSE status
                    END,
                    terminal_context = COALESCE(terminal_context, '{}'::jsonb) - %s::text[],
                    updated_at = %s
                WHERE id = ANY(%s::uuid[])
                """,
                (
                    list(LIVE_SESSION_STATUS_ORDER),
                    list(_TMUX_CONTEXT_KEYS),
                    now,
                    session_ids,
                ),
            )

        for session in sessions:
            transitioned = session.status in LIVE_SESSION_STATUSES
            self._notify_session_change(
                "session_expired" if transitioned else "session_updated",
                session.id,
            )
            if transitioned:
                self._notify_status_transition(
                    SessionStatusTransition.from_session(
                        session,
                        status="expired",
                        transitioned_at=now,
                    )
                )
        return session_ids

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
