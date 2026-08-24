"""Field update mixin for session storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.hub.protocol import SessionLineageMutation
from gobby.storage.session_models import Session
from gobby.terminal_ownership import TERMINAL_OWNER_STATUSES
from gobby.utils.datetime import utc_now

from ._constants import (
    TERMINAL_SESSION_STATUSES,
    ensure_system_session,
    past_terminal_revival_horizon,
    system_session_id,
    validate_session_status_transition,
)
from ._contested_expiry import clear_contested_terminal_expiry
from ._lineage_guard import repair_self_parent_session, sanitize_parent_session_id
from ._session_metadata_update import _SessionMetadataUpdateMixin
from ._summary_update import _SummaryUpdateMixin
from ._terminal_revival import _TerminalRevivalMixin
from ._title_fields import _TitleFieldMixin

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def _notify_status_transition(self, transition: SessionStatusTransition) -> None: ...


class _FieldUpdateMixin(
    _SessionMetadataUpdateMixin,
    _SummaryUpdateMixin,
    _TitleFieldMixin,
    _TerminalRevivalMixin,
):
    def update_status(self: _ManagerState, session_id: str, status: str) -> Session | None:
        """Persist a session status change and return the reloaded row.

        Storage-layer callers use this when they need the updated Session back.
        Service-style callers that only need a success flag should use
        SessionManager.update_session_status().
        """
        current = self.get(session_id)
        validate_session_status_transition(current.status if current else None, status)
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET status = %s, updated_at = %s WHERE id = %s",
                (status, now, session_id),
            )
        # Any status write settles the expiry a marker was describing, so the
        # marker never outlives the one it was written for. mark_session_expired
        # records its own after this returns, which is what keeps a stale marker
        # from shielding a later, final expiry (#20837).
        clear_contested_terminal_expiry(self.db, session_id)
        updated = self.get(session_id)
        if updated is not None:
            event = "session_expired" if status == "expired" else "session_updated"
            self._notify_session_change(event, session_id)
            if current is not None and current.status != status:
                self._notify_status_transition(
                    SessionStatusTransition.from_session(updated, transitioned_at=now)
                )
        return updated

    def update_status_if_non_terminal(
        self: _ManagerState,
        session_id: str,
        status: str,
    ) -> Session | None:
        """Persist a status only while the stored session remains non-terminal."""
        validate_session_status_transition(None, status)
        current = self.get(session_id)
        now = utc_now()
        with self.db.transaction():
            cursor = self.db.execute(
                """
                UPDATE sessions
                SET status = %s, updated_at = %s
                WHERE id = %s AND status != ALL(%s)
                """,
                (status, now, session_id, list(TERMINAL_SESSION_STATUSES)),
            )
        if cursor.rowcount <= 0:
            return None

        updated = self.get(session_id)
        if updated is not None:
            event = "session_expired" if status == "expired" else "session_updated"
            self._notify_session_change(event, session_id)
            if current is not None and current.status != status:
                self._notify_status_transition(
                    SessionStatusTransition.from_session(updated, transitioned_at=now)
                )
        return updated

    def update_status_from_activity(
        self: _ManagerState,
        session_id: str,
        status: str,
    ) -> Session | None:
        """Persist an active or paused status backed by confirmed session activity."""
        if status not in {"active", "paused"}:
            raise ValueError("Confirmed activity status must be 'active' or 'paused'")

        current = self.get(session_id)
        if current is not None and past_terminal_revival_horizon(current):
            return current
        now = utc_now()
        with self.db.transaction():
            cursor = self.db.execute(
                """
                UPDATE sessions
                SET status = %s,
                    transcript_processed = FALSE,
                    updated_at = %s,
                    last_activity = %s
                WHERE id = %s
                  AND status != 'deleted'
                """,
                (status, now, now, session_id),
            )
            if cursor.rowcount == 0:
                return None

            updated = self.get(session_id)
            if updated is not None:
                self._notify_session_change("session_updated", session_id)
                if current is not None and current.status != status:
                    self._notify_status_transition(
                        SessionStatusTransition.from_session(updated, transitioned_at=now)
                    )
            return updated

    def activate_web_chat_session(self: _ManagerState, session_id: str) -> Session | None:
        """Activate a durable web-chat row after its runtime starts successfully.

        This is the sole lifecycle path that may revive an expired web-chat row.
        Terminal sessions and deleted conversations remain unchanged.
        """
        current = self.get(session_id)
        if current is None:
            return None
        if current.session_type != "web_chat" or current.status == "deleted":
            return current
        if current.status == "active":
            return current

        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE sessions
                SET status = 'active', updated_at = %s, last_activity = %s
                WHERE id = %s
                AND session_type = 'web_chat'
                AND status != 'deleted'
                """,
                (now, now, session_id),
            )
        updated = self.get(session_id)
        if updated is not None and updated.status == "active":
            self._notify_session_change("session_updated", session_id)
            self._notify_status_transition(
                SessionStatusTransition.from_session(updated, transitioned_at=now)
            )
        return updated

    def expire_if_active(self: _ManagerState, session_id: str) -> Session | None:
        """Expire an eligible terminal session without overwriting a newer status."""
        now = utc_now()
        with self.db.transaction():
            cursor = self.db.execute(
                """
                UPDATE sessions
                SET status = 'expired', updated_at = %s
                WHERE id = %s AND status = ANY(%s)
                """,
                (now, session_id, list(TERMINAL_OWNER_STATUSES)),
            )
        if cursor.rowcount <= 0:
            return None
        self._notify_session_change("session_expired", session_id)
        updated = self.get(session_id)
        if updated is not None:
            self._notify_status_transition(
                SessionStatusTransition.from_session(updated, transitioned_at=now)
            )
        return updated

    def update_parent_session_id(
        self: _ManagerState, session_id: str, parent_session_id: str | None
    ) -> Session | None:
        """Update the parent session ID, using None to clear it."""
        if parent_session_id == system_session_id():
            ensure_system_session(self.db)
        now = utc_now()
        with self.db.transaction_immediate(SessionLineageMutation()) as conn:
            sanitized_parent_session_id = sanitize_parent_session_id(
                conn,
                child_session_id=session_id,
                parent_session_id=parent_session_id,
                context="parent session update",
            )
            if sanitized_parent_session_id is None:
                repair_self_parent_session(conn, session_id=session_id, now=now)

            conn.execute(
                "UPDATE sessions SET parent_session_id = %s, updated_at = %s WHERE id = %s",
                (sanitized_parent_session_id, now, session_id),
            )
        updated = self.get(session_id)
        self._notify_session_change("session_updated", session_id)
        return updated
