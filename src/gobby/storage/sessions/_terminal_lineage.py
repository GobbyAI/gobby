"""Terminal clear-lineage binding and successor discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from gobby.sessions.contested_expiry import ContestedExpiryCause
from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.session_models import Session

from ._constants import LIVE_SESSION_STATUSES
from ._contested_expiry import read_session_variables

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...

    def update_parent_session_id(
        self, session_id: str, parent_session_id: str | None
    ) -> Session | None: ...

    def mark_session_expired(self, session_id: str, *, cause: ContestedExpiryCause) -> bool: ...

    def _notify_status_transition(self, transition: SessionStatusTransition) -> None: ...


class _TerminalLineageMixin:
    def bind_native_clear_successor(self: _ManagerState, successor_id: str) -> Session | None:
        """Link a native clear to its terminal predecessor and notify routing.

        SessionEnd may have expired the predecessor before SessionStart arrives.
        Publish after the link even when expiry itself is no longer a transition.
        Staged clears retain their marker-driven binding and routing contract.
        """
        from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
        from gobby.sessions.handoff_identity import terminal_process_contexts_match

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
              AND status IN ('active', 'paused', 'waiting', 'idle', 'expired')
              AND terminal_context IS NOT NULL AND id <> %s
              AND created_at <= %s
              AND terminal_context ->> 'parent_pid' = %s
            ORDER BY last_activity DESC, id DESC
            LIMIT 250
            """,
            (
                successor.project_id,
                successor.machine_id,
                successor.source,
                successor.agent_depth,
                successor.id,
                successor.created_at,
                str(successor.terminal_context["parent_pid"])
                if successor.terminal_context.get("parent_pid")
                else None,
            ),
        )
        for row in rows:
            predecessor = Session.from_row(row)
            if not terminal_process_contexts_match(
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
                self.update_parent_session_id(successor.id, None)
                return None
            ended = self.get(predecessor.id)
            if ended is not None:
                self._notify_status_transition(SessionStatusTransition.from_session(ended))
            return ended
        return None


def clear_successor_of(db: HubDatabase, current: Session) -> str | None:
    """Return the session that succeeded this row through a clear, if any.

    A staged clear names its successor in the consumed clear marker. A native clear has
    no marker: its successor links this row as parent and runs in the same CLI process.
    A spawned child also links its parent but runs in its own process, so it never
    matches.
    """
    # Module-level imports would be circular: both modules import this package.
    from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
    from gobby.sessions.handoff_identity import terminal_process_contexts_match

    variables = read_session_variables(db, current.id, keys=(CLEAR_ATTEMPT_VARIABLE,))
    marker = variables.get(CLEAR_ATTEMPT_VARIABLE) if variables else None
    consumed_by = marker.get("consumed_by") if isinstance(marker, dict) else None
    if isinstance(consumed_by, str) and consumed_by and consumed_by != current.id:
        return consumed_by
    if not current.terminal_context:
        return None
    rows = db.fetchall(
        """
        SELECT id, terminal_context FROM sessions
        WHERE parent_session_id = %s AND id <> %s AND source = %s
          AND session_type = 'terminal' AND status = ANY(%s)
        """,
        (current.id, current.id, current.source, list(LIVE_SESSION_STATUSES)),
    )
    for row in rows:
        if terminal_process_contexts_match(row["terminal_context"], current.terminal_context):
            return str(row["id"])
    return None
