"""Bulk update mixin for session storage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from gobby.storage.session_models import Session

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol


class _ManagerState(Protocol):
    db: DatabaseProtocol
    _VALID_CHAT_MODES: ClassVar[set[str]]
    _VALID_SESSION_TYPES: ClassVar[set[str]]
    _VALID_TITLE_SOURCES: ClassVar[set[str]]

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...


class _BulkUpdateMixin:
    def update(
        self: _ManagerState,
        session_id: str,
        *,
        external_id: str | None = None,
        source: str | None = None,
        model: str | None = None,
        chat_mode: str | None = None,
        session_type: str | None = None,
        transcript_path: str | None = None,
        status: str | None = None,
        title: str | None = None,
        title_source: str | None = None,
        git_branch: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        project_id: str | None = None,
        sandbox_enabled: bool | None = None,
        sandbox_policy_hash: str | None = None,
    ) -> Session | None:
        """
        Update multiple session fields at once.

        Args:
            session_id: Session ID to update
            external_id: New external ID (optional)
            source: New provider/source (optional)
            model: New model identifier (optional)
            chat_mode: New chat mode (optional)
            session_type: New session type (optional)
            transcript_path: New transcript path (optional)
            status: New status (optional)
            title: New title (optional)
            title_source: New title provenance (optional)
            git_branch: New git branch (optional)
            terminal_context: New terminal context (optional)
            project_id: New project ID (optional)
            sandbox_enabled: Whether the session runtime is sandboxed (optional)
            sandbox_policy_hash: Stable daemon-owned sandbox policy hash (optional)

        Returns:
            Updated Session or None if not found
        """
        values: dict[str, Any] = {}

        if external_id is not None:
            values["external_id"] = external_id
        if source is not None:
            values["source"] = source
        if model is not None:
            values["model"] = model
        if chat_mode is not None:
            if chat_mode not in self._VALID_CHAT_MODES:
                raise ValueError(
                    f"Invalid chat_mode {chat_mode!r}. Must be one of: {', '.join(sorted(self._VALID_CHAT_MODES))}"
                )
            values["chat_mode"] = chat_mode
        if session_type is not None:
            if session_type not in self._VALID_SESSION_TYPES:
                raise ValueError(
                    f"Invalid session_type {session_type!r}. Must be one of: {', '.join(sorted(self._VALID_SESSION_TYPES))}"
                )
            values["session_type"] = session_type
        if transcript_path is not None:
            values["transcript_path"] = transcript_path
        if status is not None:
            values["status"] = status
        if title is not None:
            values["title"] = title
        if title_source is not None:
            if title_source not in self._VALID_TITLE_SOURCES:
                raise ValueError(
                    f"Invalid title_source {title_source!r}. Must be one of: {', '.join(sorted(self._VALID_TITLE_SOURCES))}"
                )
            values["title_source"] = title_source
        if git_branch is not None:
            values["git_branch"] = git_branch
        if terminal_context is not None:
            values["terminal_context"] = json.dumps(terminal_context)
        if project_id is not None:
            values["project_id"] = project_id
        if sandbox_enabled is not None:
            values["sandbox_enabled"] = int(sandbox_enabled)
        if sandbox_policy_hash is not None:
            values["sandbox_policy_hash"] = sandbox_policy_hash

        if not values:
            return self.get(session_id)

        values["updated_at"] = datetime.now(UTC).isoformat()

        with self.db.transaction():
            self.db.safe_update("sessions", values, "id = ?", (session_id,))
        updated = self.get(session_id)
        if updated is not None:
            event = "session_expired" if status == "expired" else "session_updated"
            self._notify_session_change(event, session_id)
        return updated

    def update_stats(
        self: _ManagerState,
        session_id: str,
        message_count: int | None = None,
        turn_count: int | None = None,
        tool_call_count: int | None = None,
        last_assistant_content: str | None = None,
    ) -> Session | None:
        """Update session stats columns.

        Args:
            session_id: Session ID
            message_count: Total message count (optional)
            turn_count: Assistant turn count (optional)
            tool_call_count: Tool call count (optional)
            last_assistant_content: Last assistant text content (optional)

        Returns:
            Updated session or None if not found
        """
        values: dict[str, Any] = {}
        if message_count is not None:
            values["message_count"] = message_count
        if turn_count is not None:
            values["turn_count"] = turn_count
        if tool_call_count is not None:
            values["tool_call_count"] = tool_call_count
        if last_assistant_content is not None:
            values["last_assistant_content"] = last_assistant_content

        if not values:
            return self.get(session_id)

        values["updated_at"] = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.safe_update("sessions", values, "id = ?", (session_id,))
        return self.get(session_id)

    def recalculate_stats(self: _ManagerState, session_id: str) -> Session | None:
        """Recalculate session stats from session_messages table.

        Args:
            session_id: Session ID

        Returns:
            Updated session or None if not found
        """
        if self.get(session_id) is None:
            return None

        sql = """
        UPDATE sessions SET
          message_count = (SELECT COUNT(*) FROM session_messages WHERE session_id = sessions.id),
          turn_count = (SELECT COUNT(*) FROM session_messages WHERE session_id = sessions.id AND role = 'assistant'),
          tool_call_count = (SELECT COUNT(*) FROM session_messages WHERE session_id = sessions.id AND tool_name IS NOT NULL),
          last_assistant_content = (SELECT content FROM session_messages WHERE session_id = sessions.id AND role = 'assistant' AND tool_name IS NULL ORDER BY message_index DESC LIMIT 1),
          updated_at = ?
        WHERE id = ?
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(sql, (now, session_id))
        return self.get(session_id)
