"""Bulk update mixin for session storage."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.hub.protocol import SessionSeqMutation
from gobby.storage.session_models import Session
from gobby.utils.datetime import utc_now

from ._constants import validate_session_status_transition
from ._title_defaults import manual_title_source
from ._title_update import TitleMutationResult, apply_title_mutation
from ._update_sentinel import UNSET, UnsetType, is_set
from ._upsert import is_session_unique_conflict

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase, Transaction


class _ManagerState(Protocol):
    db: HubDatabase
    _VALID_CHAT_MODES: ClassVar[set[str]]
    _VALID_SESSION_TYPES: ClassVar[set[str]]
    _VALID_TITLE_SOURCES: ClassVar[set[str]]

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def _notify_status_transition(self, transition: SessionStatusTransition) -> None: ...

    def _run_title_change_side_effects(self, updated: Session, title: str) -> None: ...


def _apply_session_update(
    manager: _ManagerState,
    conn: Transaction,
    session_id: str,
    values: dict[str, Any],
    terminal_context: dict[str, Any],
    updated_at: datetime,
    *,
    title_is_set: bool,
    title: str | None,
    title_source_is_set: bool,
    title_source: str | None,
) -> TitleMutationResult | None:
    if values:
        manager.db.safe_update("sessions", values, "id = %s", (session_id,))
    if terminal_context:
        conn.execute(
            """
            UPDATE sessions
            SET terminal_context = COALESCE(terminal_context, '{}'::jsonb) || %s::jsonb,
                updated_at = %s
            WHERE id = %s
            """,
            (json.dumps(terminal_context), updated_at, session_id),
        )
    return apply_title_mutation(
        conn,
        session_id,
        title_is_set=title_is_set,
        title=title,
        title_source_is_set=title_source_is_set,
        title_source=title_source,
        updated_at=updated_at,
    )


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
        transcript_path: str | None | UnsetType = UNSET,
        status: str | None = None,
        title: str | None | UnsetType = UNSET,
        title_source: str | None | UnsetType = UNSET,
        git_branch: str | None | UnsetType = UNSET,
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
            transcript_path: New transcript path; None clears it
            status: New status (optional)
            title: New title; None clears it
            title_source: New title provenance; None clears it
            git_branch: New git branch; None clears it
            terminal_context: New terminal context (optional)
            project_id: New project ID (optional)
            sandbox_enabled: Whether the session runtime is sandboxed (optional)
            sandbox_policy_hash: Stable daemon-owned sandbox policy hash (optional)

        Returns:
            Updated Session or None if not found
        """
        values: dict[str, Any] = {}
        current = self.get(session_id)

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
        if is_set(transcript_path):
            values["transcript_path"] = transcript_path
        if status is not None:
            validate_session_status_transition(current.status if current else None, status)
            values["status"] = status
        if is_set(title) and not is_set(title_source):
            title_source = manual_title_source(title)
        if is_set(title_source):
            if title_source is not None and title_source not in self._VALID_TITLE_SOURCES:
                raise ValueError(
                    f"Invalid title_source {title_source!r}. Must be one of: {', '.join(sorted(self._VALID_TITLE_SOURCES))}"
                )
        if is_set(title):
            title_is_set = True
            title_value = title
        else:
            title_is_set = False
            title_value = None
        if is_set(title_source):
            title_source_is_set = True
            title_source_value = title_source
        else:
            title_source_is_set = False
            title_source_value = None
        if is_set(git_branch):
            values["git_branch"] = git_branch
        incoming_terminal_context = (
            {key: value for key, value in terminal_context.items() if value is not None}
            if terminal_context
            else {}
        )
        if project_id is not None:
            values["project_id"] = project_id
        if sandbox_enabled is not None:
            values["sandbox_enabled"] = bool(sandbox_enabled)
        if sandbox_policy_hash is not None:
            values["sandbox_policy_hash"] = sandbox_policy_hash

        if (
            not values
            and not incoming_terminal_context
            and not title_is_set
            and not title_source_is_set
        ):
            return self.get(session_id)

        updated_at = utc_now()
        if values:
            values["updated_at"] = updated_at

        title_mutation: TitleMutationResult | None = None
        try:
            if current is not None and project_id is not None and current.project_id != project_id:
                with self.db.transaction_immediate(
                    SessionSeqMutation(project_id=project_id)
                ) as conn:
                    max_seq_row = conn.execute(
                        "SELECT MAX(seq_num) AS max_seq FROM sessions WHERE project_id = %s",
                        (project_id,),
                    ).fetchone()
                    values["seq_num"] = ((max_seq_row["max_seq"] if max_seq_row else None) or 0) + 1
                    title_mutation = _apply_session_update(
                        self,
                        conn,
                        session_id,
                        values,
                        incoming_terminal_context,
                        updated_at,
                        title_is_set=title_is_set,
                        title=title_value,
                        title_source_is_set=title_source_is_set,
                        title_source=title_source_value,
                    )
            else:
                with self.db.transaction() as conn:
                    title_mutation = _apply_session_update(
                        self,
                        conn,
                        session_id,
                        values,
                        incoming_terminal_context,
                        updated_at,
                        title_is_set=title_is_set,
                        title=title_value,
                        title_source_is_set=title_source_is_set,
                        title_source=title_source_value,
                    )
        except Exception as exc:
            if current is None or not is_session_unique_conflict(exc):
                raise
            conflicting = _conflicting_web_chat_session(self, current, values)
            if conflicting is None:
                raise
            with self.db.transaction() as conn:
                title_mutation = _apply_session_update(
                    self,
                    conn,
                    conflicting.id,
                    values,
                    incoming_terminal_context,
                    updated_at,
                    title_is_set=title_is_set,
                    title=title_value,
                    title_source_is_set=title_source_is_set,
                    title_source=title_source_value,
                )
            updated = self.get(conflicting.id)
            if updated is not None:
                mutation_applied = title_mutation is not None and title_mutation.applied
                if values or incoming_terminal_context or mutation_applied:
                    self._notify_session_change("session_updated", conflicting.id)
                if status is not None and conflicting.status != updated.status:
                    self._notify_status_transition(
                        SessionStatusTransition.from_session(
                            updated,
                            transitioned_at=updated_at,
                        )
                    )
                if title_mutation is not None and title_mutation.title_changed:
                    self._run_title_change_side_effects(updated, updated.title or "")
            return updated
        updated = self.get(session_id)
        if updated is not None:
            event = "session_expired" if status == "expired" else "session_updated"
            mutation_applied = title_mutation is not None and title_mutation.applied
            if values or incoming_terminal_context or mutation_applied:
                self._notify_session_change(event, session_id)
            if status is not None and current is not None and current.status != updated.status:
                self._notify_status_transition(
                    SessionStatusTransition.from_session(updated, transitioned_at=updated_at)
                )
            if title_mutation is not None and title_mutation.title_changed:
                self._run_title_change_side_effects(updated, updated.title or "")
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

        values["updated_at"] = utc_now()
        # Transcript growth is confirmed activity; stat rewrites that don't
        # raise a counter (sidecar rehydration, idle re-processing) are not.
        current = self.get(session_id)
        if current is not None and (
            (message_count is not None and message_count > (current.message_count or 0))
            or (turn_count is not None and turn_count > (current.turn_count or 0))
            or (tool_call_count is not None and tool_call_count > (current.tool_call_count or 0))
        ):
            values["last_activity"] = values["updated_at"]
        with self.db.transaction():
            self.db.safe_update("sessions", values, "id = %s", (session_id,))
        return self.get(session_id)


def _conflicting_web_chat_session(
    manager: _ManagerState,
    current: Session,
    values: dict[str, Any],
) -> Session | None:
    target_session_type = values.get("session_type", current.session_type)
    if target_session_type != "web_chat":
        return None

    external_id = values.get("external_id", current.external_id)
    source = values.get("source", current.source)
    project_id = values.get("project_id", current.project_id)
    if not isinstance(external_id, str) or not isinstance(source, str):
        return None

    if project_id is None:
        row = manager.db.fetchone(
            """
            SELECT *
              FROM sessions
             WHERE external_id = %s
               AND machine_id = %s
               AND source = %s
               AND project_id IS NULL
               AND session_type = 'web_chat'
               AND id != %s
             LIMIT 1
            """,
            (external_id, current.machine_id, source, current.id),
        )
    else:
        row = manager.db.fetchone(
            """
            SELECT *
              FROM sessions
             WHERE external_id = %s
               AND machine_id = %s
               AND source = %s
               AND project_id = %s
               AND session_type = 'web_chat'
               AND id != %s
             LIMIT 1
            """,
            (external_id, current.machine_id, source, project_id, current.id),
        )
    return Session.from_row(row) if row else None
