"""Terminal ownership revival for expired session rows."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Protocol

from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.session_models import Session
from gobby.storage.terminals import TerminalManager
from gobby.terminal_ownership import (
    TERMINAL_OWNER_STATUSES,
    recorded_process_exited,
)
from gobby.utils.datetime import utc_now

from ._constants import LIVE_SESSION_STATUSES, get_logger, past_terminal_revival_horizon
from ._contested_expiry import (
    clear_contested_terminal_expiry,
    read_session_variables,
)
from ._operator_claim_hold import clear_operator_claim_hold
from ._terminal_lineage import (
    FRESH_SESSION_RETIRED_VARIABLE,
    clear_successor_of,
    live_row_of_same_cli,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _RevivalHost(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def _notify_status_transition(self, transition: SessionStatusTransition) -> None: ...

    def revive_expired_terminal_session(
        self,
        session_id: str,
        current: Session | None = None,
        *,
        trigger: RevivalTrigger = ...,
    ) -> Session | None: ...


class RevivalTrigger(Enum):
    """What brought fresh evidence that an expired terminal session lives."""

    # Hook activity: past the revival horizon the row stays expired.
    ACTIVITY = "activity"
    # An explicit register_session call, or a hook from the CLI process the row stores
    # as owner: the caller asserts the session is live, so the horizon does not apply.
    # Clear successors, exited CLIs and pane contests still do.
    REGISTRATION = "registration"


class _TerminalRevivalMixin:
    def revive_registered_terminal_session(
        self: _RevivalHost, session: Session, *, caller_session_id: str | None
    ) -> Session:
        """Reactivate an expired terminal row that its own session registers again.

        Only the session itself may revive the row this way: a registration from another
        session, or from no session, leaves it expired. Returns the row as it stands
        afterwards, still expired when ownership refuses it.
        """
        if session.session_type != "terminal" or session.status != "expired":
            return session
        if caller_session_id != session.id:
            return session
        revived = self.revive_expired_terminal_session(
            session.id, session, trigger=RevivalTrigger.REGISTRATION
        )
        return revived or session

    def revive_terminal_session_for_owner(self: _RevivalHost, session_id: str) -> bool:
        """Reactivate an expired terminal row for the CLI process it stores as owner.

        The caller has matched that process by pid and start time, so the row's age does
        not count against it. Returns whether the row is live afterwards; it stays
        expired when a clear successor or a pane contest refuses it.
        """
        revived = self.revive_expired_terminal_session(
            session_id, trigger=RevivalTrigger.REGISTRATION
        )
        return revived is not None and revived.status in LIVE_SESSION_STATUSES

    def revive_expired_terminal_session(
        self: _RevivalHost,
        session_id: str,
        current: Session | None = None,
        *,
        trigger: RevivalTrigger = RevivalTrigger.ACTIVITY,
    ) -> Session | None:
        """Reconcile terminal ownership when fresh activity arrives.

        Interactive claims require foreground-process validation when they
        compete. Spawned-only claims retain newest-created ownership.
        ``current`` is this session's row when the caller already loaded it.
        """
        if current is None or current.id != session_id:
            current = self.get(session_id)
        if current is None:
            return None
        if current.session_type != "terminal":
            return current
        if current.status not in {*TERMINAL_OWNER_STATUSES, "expired"}:
            return current

        variables = read_session_variables(
            self.db, current.id, keys=(FRESH_SESSION_RETIRED_VARIABLE,)
        )
        if variables and variables.get(FRESH_SESSION_RETIRED_VARIABLE) is True:
            return current

        successor_id = clear_successor_of(self.db, current)
        if successor_id is not None:
            # The CLI cleared this session and a successor bound to the pane, so
            # the row is finished by construction. A late hook still carrying its
            # external id must not bring it back beside the successor: two live
            # rows on one terminal context tie caller resolution and every
            # implicit proxy call in the pane fails with SESSION_REQUIRED (#22525).
            get_logger().info(
                "Suppressed revival of cleared terminal session %s; its clear marker "
                "was consumed by %s",
                session_id,
                successor_id,
                extra={
                    "event": "terminal_session_revival_suppressed_cleared",
                    "session_id": session_id,
                    "clear_successor_session_id": successor_id,
                    "status": current.status,
                },
            )
            return current

        if current.status == "expired" and recorded_process_exited(current):
            # Activity from a hook that names no seat, such as a shared Codex
            # app-server, cannot bring back a row whose CLI exited; the live row
            # would only draw wakes and handoffs into its old pane. A CLI that
            # merely lost the foreground, as under a nested CLI, still revives.
            get_logger().info(
                "Suppressed revival of terminal session %s; its recorded CLI process exited",
                session_id,
                extra={"event": "terminal_session_revival_suppressed_process_exited"},
            )
            return current

        if current.status == "expired" and trigger is RevivalTrigger.ACTIVITY:
            holder_id = live_row_of_same_cli(self.db, current)
            if holder_id is not None:
                # A late or replayed hook is no word from the CLI about where it is
                # now, and its live row says it left this conversation. Only its own
                # start brings the row back; two live rows on one CLI would tie
                # caller resolution as two on one pane do (#26073).
                get_logger().info(
                    "Suppressed revival of terminal session %s; its CLI holds %s",
                    session_id,
                    holder_id,
                    extra={
                        "event": "terminal_session_revival_suppressed_cli_moved_on",
                        "session_id": session_id,
                        "live_session_id": holder_id,
                    },
                )
                return current

        past_horizon = trigger is RevivalTrigger.ACTIVITY and past_terminal_revival_horizon(current)
        if current.status != "expired" or past_horizon:
            return current

        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE sessions
                SET status = 'active',
                    transcript_processed = FALSE,
                    updated_at = %s,
                    last_activity = %s
                WHERE id = %s
                  AND status = 'expired'
                  AND session_type = 'terminal'
                """,
                (now, now, session_id),
            )
        updated = self.get(session_id)
        if updated is not None and updated.status == "active":
            clear_contested_terminal_expiry(self.db, session_id)
            # The parked seat is back, so the operator hold has done its job.
            clear_operator_claim_hold(self.db, session_id)
            # Expiry released the gterm pane, and a surviving CLI sends no
            # SessionStart to bind it again, so revival rebinds it here.
            context = updated.terminal_context or {}
            native_terminal_id = context.get("gobby_terminal_id")
            if isinstance(native_terminal_id, str) and native_terminal_id:
                TerminalManager(self.db).bind_session(
                    native_terminal_id, session_id, updated.project_id
                )
            self._notify_session_change("session_updated", session_id)
            self._notify_status_transition(
                SessionStatusTransition.from_session(updated, transitioned_at=now)
            )
        return updated
