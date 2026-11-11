"""Session metadata update mixin for session storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol

from gobby.storage.session_models import Session
from gobby.storage.sessions._constants import STATUS_LINE_MAX_LENGTH
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


def normalize_status_line(status_line: str | None) -> str | None:
    """Collapse whitespace to single spaces and drop non-printable characters.

    The sidebar draws the line into terminal cells, so an ESC, a CR or a bell from a
    seat must not reach the operator's terminal as-is. A blank result means clear.
    """
    collapsed = " ".join((status_line or "").split())
    return "".join(ch for ch in collapsed if ch.isprintable()) or None


class _ManagerState(Protocol):
    db: HubDatabase
    _VALID_CHAT_MODES: ClassVar[set[str]]

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...


class _SessionMetadataUpdateMixin:
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
            if chat_mode != "plan":
                from gobby.workflows.state_manager import SessionVariableManager

                # UI mode changes can leave and re-enter Plan Mode between prompts.
                SessionVariableManager(self.db).merge_variables(
                    session_id, {"plan_skill_directive_delivered": False}
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

    def set_status_line(self: _ManagerState, session_id: str, status_line: str | None) -> bool:
        """Set or clear a session's one-line status; a blank line clears it.

        The line is normalized to one printable line. A line past STATUS_LINE_MAX_LENGTH
        after normalization is refused with ValueError and the stored line stays as it
        was. updated_at is left alone: the line labels the work and is not activity.

        Returns:
            True when the session row exists and was written, False otherwise.
        """
        text = normalize_status_line(status_line)
        if text is not None and len(text) > STATUS_LINE_MAX_LENGTH:
            raise ValueError(f"status_line must be at most {STATUS_LINE_MAX_LENGTH} characters")
        with self.db.transaction():
            cursor = self.db.execute(
                "UPDATE sessions SET status_line = %s WHERE id = %s",
                (text, session_id),
            )
        if cursor.rowcount <= 0:
            return False
        self._notify_session_change("session_updated", session_id)
        return True
