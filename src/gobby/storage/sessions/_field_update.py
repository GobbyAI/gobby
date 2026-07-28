"""Field update mixin for session storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol

from gobby.storage.hub.protocol import SessionLineageMutation
from gobby.storage.session_models import Session
from gobby.terminal_ownership import (
    terminal_session_creation_order,
    terminal_session_identity,
)
from gobby.utils.datetime import utc_now

from ._bootstrap import TitleChangeCallback
from ._constants import (
    SYSTEM_SESSION_ID,
    ensure_system_session,
    get_logger,
    validate_session_status_transition,
)
from ._lineage_guard import repair_self_parent_session, sanitize_parent_session_id
from ._summary_update import _SummaryUpdateMixin
from ._title_defaults import MANUAL_TITLE_SOURCE
from ._title_update import apply_title_mutation

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(Protocol):
    db: HubDatabase
    _title_listeners: list[TitleChangeCallback]
    _VALID_CHAT_MODES: ClassVar[set[str]]
    _VALID_TITLE_SOURCES: ClassVar[set[str]]

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def _run_title_change_side_effects(self, updated: Session, title: str) -> None: ...


class _FieldUpdateMixin(_SummaryUpdateMixin):
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
        updated = self.get(session_id)
        if updated is not None:
            event = "session_expired" if status == "expired" else "session_updated"
            self._notify_session_change(event, session_id)
        return updated

    def update_status_from_activity(
        self: _ManagerState,
        session_id: str,
        status: str,
    ) -> Session | None:
        """Persist an active or paused status backed by confirmed session activity."""
        if status not in {"active", "paused"}:
            raise ValueError("Confirmed activity status must be 'active' or 'paused'")

        now = utc_now()
        with self.db.transaction():
            cursor = self.db.execute(
                """
                UPDATE sessions
                SET status = %s,
                    transcript_processed = FALSE,
                    updated_at = %s
                WHERE id = %s
                  AND status != 'deleted'
                """,
                (status, now, session_id),
            )
            if cursor.rowcount == 0:
                return None

            updated = self.get(session_id)
            if updated is not None:
                self._notify_session_change("session_updated", session_id)
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
                SET status = 'active', updated_at = %s
                WHERE id = %s
                AND session_type = 'web_chat'
                AND status != 'deleted'
                """,
                (now, session_id),
            )
        updated = self.get(session_id)
        if updated is not None and updated.status == "active":
            self._notify_session_change("session_updated", session_id)
        return updated

    def expire_if_active(self: _ManagerState, session_id: str) -> Session | None:
        """Expire an active or paused session without overwriting a newer status."""
        now = utc_now()
        with self.db.transaction():
            cursor = self.db.execute(
                """
                UPDATE sessions
                SET status = 'expired', updated_at = %s
                WHERE id = %s AND status IN ('active', 'paused')
                """,
                (now, session_id),
            )
        if cursor.rowcount <= 0:
            return None
        self._notify_session_change("session_expired", session_id)
        return self.get(session_id)

    def revive_expired_terminal_session(self: _ManagerState, session_id: str) -> Session | None:
        """Reconcile terminal ownership when fresh activity arrives.

        Hook and transcript activity are stronger liveness evidence than a stale
        parent PID. The newest session created for a machine/socket/pane owns
        that terminal, so delayed historical activity cannot revive a
        superseded row.
        """
        current = self.get(session_id)
        if current is None:
            return None
        if current.session_type != "terminal":
            return current

        identity = terminal_session_identity(current)
        if identity is None:
            if current.status != "expired":
                return current

            now = utc_now()
            with self.db.transaction():
                self.db.execute(
                    """
                    UPDATE sessions
                    SET status = 'active',
                        transcript_processed = FALSE,
                        updated_at = %s
                    WHERE id = %s
                      AND status = 'expired'
                      AND session_type = 'terminal'
                    """,
                    (now, session_id),
                )
            updated = self.get(session_id)
            if updated is not None and updated.status == "active":
                self._notify_session_change("session_updated", session_id)
            return updated

        machine_id, socket_identity, pane = identity
        now = utc_now()
        status_changes: list[tuple[Session, str]] = []
        owner: Session | None = None
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM sessions
                WHERE machine_id = %s
                  AND session_type = 'terminal'
                  AND terminal_context ->> 'tmux_pane' = %s
                ORDER BY created_at, id
                FOR UPDATE
                """,
                (machine_id, pane),
            ).fetchall()
            matching = [
                candidate
                for row in rows
                if terminal_session_identity(candidate := Session.from_row(row)) == identity
            ]
            if not matching:
                return self.get(session_id)

            owner = max(matching, key=terminal_session_creation_order)
            reset_target_transcript = current.status == "expired"
            for candidate in matching:
                desired_status = candidate.status
                if candidate.id == owner.id and candidate.status == "expired":
                    desired_status = "active"
                elif candidate.id != owner.id and candidate.status in {
                    "active",
                    "paused",
                    "handoff_ready",
                }:
                    desired_status = "expired"

                reset_transcript = reset_target_transcript and candidate.id == session_id
                if desired_status == candidate.status and not reset_transcript:
                    continue

                conn.execute(
                    """
                    UPDATE sessions
                    SET status = %s,
                        transcript_processed = CASE
                            WHEN %s THEN FALSE
                            ELSE transcript_processed
                        END,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (desired_status, reset_transcript, now, candidate.id),
                )
                if desired_status != candidate.status:
                    status_changes.append((candidate, desired_status))

        if owner is None:
            return self.get(session_id)

        logger = get_logger()
        if current.status == "expired" and current.id != owner.id:
            logger.debug(
                "Suppressed revival of superseded terminal session %s; owner is %s",
                current.id,
                owner.id,
                extra={
                    "event": "terminal_session_revival_suppressed",
                    "session_id": current.id,
                    "terminal_owner_session_id": owner.id,
                    "machine_id": machine_id,
                    "tmux_socket": socket_identity,
                    "tmux_pane": pane,
                },
            )

        for candidate, desired_status in status_changes:
            self._notify_session_change("session_updated", candidate.id)
            if desired_status == "expired":
                logger.info(
                    "Expired superseded terminal session %s; owner is %s",
                    candidate.id,
                    owner.id,
                    extra={
                        "event": "terminal_session_owner_superseded",
                        "session_id": candidate.id,
                        "terminal_owner_session_id": owner.id,
                        "machine_id": machine_id,
                        "tmux_socket": socket_identity,
                        "tmux_pane": pane,
                    },
                )

        return self.get(session_id)

    def mark_had_edits(self: _ManagerState, session_id: str) -> Session | None:
        """Mark session as having edits."""
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET had_edits = TRUE, updated_at = %s WHERE id = %s",
                (now, session_id),
            )
        return self.get(session_id)

    def clear_had_edits(self: _ManagerState, session_id: str) -> None:
        """Reset had_edits after a task is closed with a linked commit."""
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET had_edits = FALSE, updated_at = %s WHERE id = %s",
                (now, session_id),
            )

    def update_chat_mode(self: _ManagerState, session_id: str, chat_mode: str) -> None:
        """Persist the chat mode (plan, accept_edits, normal, bypass) for a session."""
        if chat_mode not in self._VALID_CHAT_MODES:
            raise ValueError(
                f"Invalid chat_mode {chat_mode!r}. Must be one of: {', '.join(sorted(self._VALID_CHAT_MODES))}"
            )
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET chat_mode = %s, updated_at = %s WHERE id = %s",
                (chat_mode, now, session_id),
            )

    def update_approved_tools(self: _ManagerState, session_id: str, tools: set[str]) -> None:
        """Persist the set of user-approved tools as JSON."""
        import json as _json

        tools_json = _json.dumps(sorted(tools)) if tools else None
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET approved_tools_json = %s, updated_at = %s WHERE id = %s",
                (tools_json, now, session_id),
            )

    def update_title(
        self: _ManagerState,
        session_id: str,
        title: str,
        *,
        title_source: str | None = MANUAL_TITLE_SOURCE,
    ) -> Session | None:
        """Update session title."""
        current = self.get(session_id)
        if current is None:
            return None
        if title_source is not None and title_source not in self._VALID_TITLE_SOURCES:
            raise ValueError(
                f"Invalid title_source {title_source!r}. Must be one of: {', '.join(sorted(self._VALID_TITLE_SOURCES))}"
            )

        now = utc_now()
        with self.db.transaction() as conn:
            mutation = apply_title_mutation(
                conn,
                session_id,
                title_is_set=True,
                title=title,
                title_source_is_set=title_source is not None,
                title_source=title_source,
                updated_at=now,
            )
        updated = self.get(session_id)
        if updated is None:
            return None
        if mutation is None or not mutation.applied:
            return updated

        self._notify_session_change("session_updated", session_id)

        if mutation.title_changed:
            self._run_title_change_side_effects(updated, updated.title or "")

        return updated

    def _run_title_change_side_effects(
        self: _ManagerState,
        updated: Session,
        title: str,
    ) -> None:
        session_id = updated.id
        try:
            from gobby.sessions.tmux_window_naming import schedule_tmux_window_rename

            schedule_tmux_window_rename(updated, title)
        except Exception:
            get_logger().warning(
                "Failed to schedule tmux title update for session %s",
                session_id,
                exc_info=True,
            )

        for listener in list(self._title_listeners):
            try:
                listener(session_id, title)
            except Exception:
                get_logger().warning(
                    "Title listener failed for session %s", session_id, exc_info=True
                )

    def update_model(self: _ManagerState, session_id: str, model: str) -> Session | None:
        """Update session model (LLM model used)."""
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET model = %s, updated_at = %s WHERE id = %s",
                (model, now, session_id),
            )
        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

    def update_parent_session_id(
        self: _ManagerState, session_id: str, parent_session_id: str | None
    ) -> Session | None:
        """Update the parent session ID, using None to clear it."""
        if parent_session_id == SYSTEM_SESSION_ID:
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
