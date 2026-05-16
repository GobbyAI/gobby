"""Field update mixin for session storage."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from gobby.storage.session_models import Session

from ._bootstrap import TitleChangeCallback
from ._constants import SYSTEM_SESSION_ID, ensure_system_session, get_logger

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol


class _ManagerState(Protocol):
    db: DatabaseProtocol
    _title_listeners: list[TitleChangeCallback]
    _VALID_CHAT_MODES: ClassVar[set[str]]
    _VALID_TITLE_SOURCES: ClassVar[set[str]]

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...


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
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, session_id),
            )
        updated = self.get(session_id)
        if updated is not None:
            event = "session_expired" if status == "expired" else "session_updated"
            self._notify_session_change(event, session_id)
        return updated

    def mark_had_edits(self: _ManagerState, session_id: str) -> Session | None:
        """Mark session as having edits."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET had_edits = 1, updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        return self.get(session_id)

    def clear_had_edits(self: _ManagerState, session_id: str) -> None:
        """Reset had_edits after a task is closed with a linked commit."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET had_edits = 0, updated_at = ? WHERE id = ?",
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
                "UPDATE sessions SET chat_mode = ?, updated_at = ? WHERE id = ?",
                (chat_mode, now, session_id),
            )

    def update_approved_tools(self: _ManagerState, session_id: str, tools: set[str]) -> None:
        """Persist the set of user-approved tools as JSON."""
        import json as _json

        tools_json = _json.dumps(sorted(tools)) if tools else None
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET approved_tools_json = ?, updated_at = ? WHERE id = ?",
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
            self.db.safe_update("sessions", values, "id = ?", (session_id,))
        updated = self.get(session_id)
        if updated is None:
            return None

        self._notify_session_change("session_updated", session_id)

        if title_changed:
            try:
                from gobby.workflows.summary_actions import schedule_tmux_window_rename

                schedule_tmux_window_rename(updated, title)
            except Exception:
                get_logger().warning(
                    "Failed to schedule tmux title update for session %s",
                    session_id,
                    exc_info=True,
                )

        if title_changed:
            for listener in list(self._title_listeners):
                try:
                    listener(session_id, title)
                except Exception:
                    get_logger().warning(
                        "Title listener failed for session %s", session_id, exc_info=True
                    )

        return updated

    def update_model(self: _ManagerState, session_id: str, model: str) -> Session | None:
        """Update session model (LLM model used)."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET model = ?, updated_at = ? WHERE id = ?",
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
                SET summary_path = COALESCE(?, summary_path),
                    summary_markdown = COALESCE(?, summary_markdown),
                    updated_at = ?
                WHERE id = ?
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
                SET digest_markdown = ?,
                    updated_at = ?
                WHERE id = ?
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
                SET last_turn_markdown = ?,
                    updated_at = ?
                WHERE id = ?
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
                SET last_digest_input_hash = ?,
                    updated_at = ?
                WHERE id = ?
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
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET parent_session_id = ?, updated_at = ? WHERE id = ?",
                (parent_session_id, now, session_id),
            )
        return self.get(session_id)
