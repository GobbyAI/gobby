"""Field update mixin for session storage."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from gobby.storage.session_models import Session

from ._bootstrap import TitleChangeCallback
from ._constants import SYSTEM_SESSION_ID, ensure_system_session, get_logger
from ._lineage_guard import repair_self_parent_session, sanitize_parent_session_id

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


class _FieldUpdateMixin:
    def update_status(self: _ManagerState, session_id: str, status: str) -> Session | None:
        """Persist a session status change and return the reloaded row.

        Storage-layer callers use this when they need the updated Session back.
        Service-style callers that only need a success flag should use
        SessionManager.update_session_status().
        """
        now = datetime.now(UTC).isoformat()
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

    def revive_expired_terminal_session(self: _ManagerState, session_id: str) -> Session | None:
        """Mark an expired terminal session active when fresh activity arrives.

        Hook and transcript activity are stronger liveness evidence than a stale
        parent PID. Reset transcript_processed too so later finalization can
        process any new transcript tail.
        """
        current = self.get(session_id)
        if current is None:
            return None
        if current.status != "expired" or current.session_type != "terminal":
            return current

        now = datetime.now(UTC).isoformat()
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

    def mark_had_edits(self: _ManagerState, session_id: str) -> Session | None:
        """Mark session as having edits."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET had_edits = TRUE, updated_at = %s WHERE id = %s",
                (now, session_id),
            )
        return self.get(session_id)

    def clear_had_edits(self: _ManagerState, session_id: str) -> None:
        """Reset had_edits after a task is closed with a linked commit."""
        now = datetime.now(UTC).isoformat()
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
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET chat_mode = %s, updated_at = %s WHERE id = %s",
                (chat_mode, now, session_id),
            )

    def update_approved_tools(self: _ManagerState, session_id: str, tools: set[str]) -> None:
        """Persist the set of user-approved tools as JSON."""
        import json as _json

        tools_json = _json.dumps(sorted(tools)) if tools else None
        now = datetime.now(UTC).isoformat()
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
        title_source: str | None = None,
    ) -> Session | None:
        """Update session title."""
        current = self.get(session_id)
        if current is None:
            return None
        if title_source is not None and title_source not in self._VALID_TITLE_SOURCES:
            raise ValueError(
                f"Invalid title_source {title_source!r}. Must be one of: {', '.join(sorted(self._VALID_TITLE_SOURCES))}"
            )

        title_changed = current.title != title
        source_changed = title_source is not None and current.title_source != title_source
        if not title_changed and not source_changed:
            return current

        now = datetime.now(UTC).isoformat()
        values: dict[str, Any] = {"updated_at": now}
        if title_changed:
            values["title"] = title
        if source_changed:
            values["title_source"] = title_source
        with self.db.transaction():
            self.db.safe_update("sessions", values, "id = %s", (session_id,))
        updated = self.get(session_id)
        if updated is None:
            return None

        self._notify_session_change("session_updated", session_id)

        if title_changed:
            self._run_title_change_side_effects(updated, title)

        return updated

    def _run_title_change_side_effects(
        self: _ManagerState,
        updated: Session,
        title: str,
    ) -> None:
        session_id = updated.id
        try:
            from gobby.workflows.summary_actions import schedule_tmux_window_rename

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

    def persist_digest_state(
        self: _ManagerState,
        session_id: str,
        *,
        last_turn_markdown: str,
        digest_markdown: str,
        last_digest_input_hash: str,
        title: str | None = None,
        title_source: str | None = None,
    ) -> Session | None:
        """Persist digest fields, optionally updating title metadata atomically."""
        current = self.get(session_id)
        if current is None:
            return None
        if title_source is not None and title_source not in self._VALID_TITLE_SOURCES:
            raise ValueError(
                f"Invalid title_source {title_source!r}. Must be one of: {', '.join(sorted(self._VALID_TITLE_SOURCES))}"
            )

        changed_title = title if title is not None and current.title != title else None
        source_changed = title_source is not None and current.title_source != title_source

        now = datetime.now(UTC).isoformat()
        values: dict[str, Any] = {
            "last_turn_markdown": last_turn_markdown,
            "digest_markdown": digest_markdown,
            "last_digest_input_hash": last_digest_input_hash,
            "updated_at": now,
        }
        if changed_title is not None:
            values["title"] = changed_title
        if source_changed:
            values["title_source"] = title_source

        set_clause = ", ".join(f"{column} = %s" for column in values)
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE sessions SET {set_clause} WHERE id = %s",  # nosec B608
                (*values.values(), session_id),
            )

        updated = self.get(session_id)
        if updated is None:
            return None

        self._notify_session_change("session_updated", session_id)
        if changed_title is not None:
            self._run_title_change_side_effects(updated, changed_title)
        return updated

    def update_model(self: _ManagerState, session_id: str, model: str) -> Session | None:
        """Update session model (LLM model used)."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET model = %s, updated_at = %s WHERE id = %s",
                (model, now, session_id),
            )
        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

    def update_summary(
        self: _ManagerState,
        session_id: str,
        summary_path: str | None = None,
        summary_markdown: str | None = None,
    ) -> Session | None:
        """Update session summary."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE sessions
                SET summary_path = COALESCE(%s, summary_path),
                    summary_markdown = COALESCE(%s, summary_markdown),
                    updated_at = %s
                WHERE id = %s
                """,
                (summary_path, summary_markdown, now, session_id),
            )
        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

    def update_digest_markdown(
        self: _ManagerState, session_id: str, digest_markdown: str
    ) -> Session | None:
        """Update session rolling digest markdown."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE sessions
                SET digest_markdown = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (digest_markdown, now, session_id),
            )
        updated = self.get(session_id)
        if updated is not None:
            self._notify_session_change("session_updated", session_id)
        return updated

    def update_last_turn_markdown(
        self: _ManagerState, session_id: str, last_turn_markdown: str
    ) -> Session | None:
        """Update session last turn markdown record."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE sessions
                SET last_turn_markdown = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (last_turn_markdown, now, session_id),
            )
        session = self.get(session_id)
        if session is not None:
            self._notify_session_change("session_updated", session_id)
        return session

    def update_last_digest_input_hash(
        self: _ManagerState, session_id: str, hash_value: str
    ) -> None:
        """Update the last digest input hash for idempotency."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                """
                UPDATE sessions
                SET last_digest_input_hash = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (hash_value, now, session_id),
            )

    def update_parent_session_id(
        self: _ManagerState, session_id: str, parent_session_id: str
    ) -> Session | None:
        """Update parent session ID."""
        if parent_session_id == SYSTEM_SESSION_ID:
            ensure_system_session(self.db)
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            sanitized_parent_session_id = sanitize_parent_session_id(
                conn,
                child_session_id=session_id,
                parent_session_id=parent_session_id,
                context="parent session update",
            )
            if sanitized_parent_session_id is None:
                repair_self_parent_session(conn, session_id=session_id, now=now)
                return self.get(session_id)

            conn.execute(
                "UPDATE sessions SET parent_session_id = %s, updated_at = %s WHERE id = %s",
                (sanitized_parent_session_id, now, session_id),
            )
        return self.get(session_id)
