"""Terminal clear-lineage binding and successor discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.session_models import Session

from ._constants import LIVE_SESSION_STATUSES
from ._contested_expiry import read_session_variables

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


FRESH_SESSION_RETIRED_VARIABLE = "fresh_session_retired"


class _ManagerState(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...

    def update_status(self, session_id: str, status: str) -> Session | None: ...

    def _notify_status_transition(self, transition: SessionStatusTransition) -> None: ...


class _TerminalLineageMixin:
    def bind_native_clear_successor(self: _ManagerState, successor_id: str) -> Session | None:
        """Retire the previous native seat without linking the fresh session to it."""
        from gobby.sessions.handoff_identity import terminal_process_contexts_match
        from gobby.sessions.handoff_retirement import retire_session_handoff
        from gobby.storage.tasks._transitions import release_task_claim_if_owned
        from gobby.workflows.state_manager import SessionVariableManager

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
              AND status IN ('active', 'paused', 'waiting', 'idle', 'awaiting_handoff', 'expired')
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
            if self.update_status(predecessor.id, "expired") is None:
                return None
            retire_session_handoff(self.db, predecessor.id)
            variables = SessionVariableManager(self.db)
            variables.merge_variables(predecessor.id, {FRESH_SESSION_RETIRED_VARIABLE: True})
            claims = self.db.fetchall(
                "SELECT id FROM tasks WHERE claimed_by_session_id = %s AND closed_at IS NULL",
                (predecessor.id,),
            )
            for task in claims:
                if (
                    release_task_claim_if_owned(self.db, task["id"], expected_owner=predecessor.id)
                    is not None
                ):
                    variables.release_task_claim(predecessor.id, task["id"])
            ended = self.get(predecessor.id)
            if ended is not None and predecessor.status == "expired":
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
    from gobby.sessions.handoff_identity import terminal_process_contexts_match
    from gobby.sessions.handoff_retirement import CLEAR_ATTEMPT_VARIABLE

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


def live_row_of_same_cli(db: HubDatabase, current: Session) -> str | None:
    """Return a live terminal row that stores this row's CLI process, if any.

    A CLI holds one conversation at a time, so such a row shows the process left this
    one: by a clear, by resuming another thread, or by starting a new one. No parent link
    is needed to say so. A row that records no process identity never matches.
    """
    # A module-level import would be circular: both modules import this package.
    from gobby.sessions.handoff_identity import terminal_process_contexts_match

    context = current.terminal_context
    if not context or not context.get("parent_pid"):
        return None
    rows = db.fetchall(
        """
        SELECT id, terminal_context FROM sessions
        WHERE machine_id IS NOT DISTINCT FROM %s AND id <> %s
          AND session_type = 'terminal' AND status = ANY(%s)
          AND terminal_context ->> 'parent_pid' = %s
        """,
        (current.machine_id, current.id, list(LIVE_SESSION_STATUSES), str(context["parent_pid"])),
    )
    for row in rows:
        if terminal_process_contexts_match(row["terminal_context"], context):
            return str(row["id"])
    return None
