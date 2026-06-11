"""Field update mixin for session storage."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from gobby.storage.session_models import Session

from ._bootstrap import TitleChangeCallback
from ._constants import SYSTEM_SESSION_ID, ensure_system_session, get_logger
from ._lineage_guard import repair_self_parent_session, sanitize_parent_session_id

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


def _encode_metadata_json(metadata_json: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(metadata_json or {}), sort_keys=True)


def _decode_metadata_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _summary_revision_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "summary_markdown": row["summary_markdown"],
        "generation_mode": row["generation_mode"],
        "source_context_hash": row["source_context_hash"],
        "source_digest_turn_count": row["source_digest_turn_count"],
        "previous_revision_id": row["previous_revision_id"],
        "metadata_json": _decode_metadata_json(row["metadata_json"]),
        "created_at": row["created_at"],
    }


class _ManagerState(Protocol):
    db: HubDatabase
    _title_listeners: list[TitleChangeCallback]
    _VALID_CHAT_MODES: ClassVar[set[str]]
    _VALID_TITLE_SOURCES: ClassVar[set[str]]

    def get(self, session_id: str) -> Session | None: ...

    def persist_summary_state(
        self,
        session_id: str,
        *,
        summary_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = None,
        source_digest_turn_count: int | None = None,
        previous_revision_id: str | None = None,
        metadata_json: Mapping[str, Any] | None = None,
        summary_path: str | None = None,
    ) -> Session | None: ...

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

    def persist_summary_state(
        self: _ManagerState,
        session_id: str,
        *,
        summary_markdown: str,
        generation_mode: str,
        source_context_hash: str | None = None,
        source_digest_turn_count: int | None = None,
        previous_revision_id: str | None = None,
        metadata_json: Mapping[str, Any] | None = None,
        summary_path: str | None = None,
    ) -> Session | None:
        """Persist summary markdown, source metadata, and a revision row atomically."""
        if source_digest_turn_count is not None and source_digest_turn_count < 0:
            raise ValueError("source_digest_turn_count must be non-negative")

        now = datetime.now(UTC).isoformat()
        revision_id = str(uuid.uuid4())

        with self.db.transaction() as conn:
            current_row = conn.execute(
                "SELECT summary_revision_id FROM sessions WHERE id = %s FOR UPDATE",
                (session_id,),
            ).fetchone()
            if current_row is None:
                return None
            previous_id = previous_revision_id
            if previous_id is None:
                previous_id = current_row["summary_revision_id"]

            conn.execute(
                """
                INSERT INTO session_summary_revisions (
                    id, session_id, summary_markdown, generation_mode,
                    source_context_hash, source_digest_turn_count,
                    previous_revision_id, metadata_json, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    revision_id,
                    session_id,
                    summary_markdown,
                    generation_mode,
                    source_context_hash,
                    source_digest_turn_count,
                    previous_id,
                    _encode_metadata_json(metadata_json),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE sessions
                SET summary_path = COALESCE(%s, summary_path),
                    summary_markdown = %s,
                    summary_revision_id = %s,
                    summary_source_context_hash = %s,
                    summary_digest_turn_count = %s,
                    summary_generation_mode = %s,
                    summary_generated_at = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    summary_path,
                    summary_markdown,
                    revision_id,
                    source_context_hash,
                    source_digest_turn_count,
                    generation_mode,
                    now,
                    now,
                    session_id,
                ),
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
        if summary_markdown is not None:
            return self.persist_summary_state(
                session_id,
                summary_markdown=summary_markdown,
                generation_mode="agent_authored",
                source_context_hash=None,
                source_digest_turn_count=None,
                metadata_json={"source": "update_summary"},
                summary_path=summary_path,
            )

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

    def get_summary_revision(
        self: _ManagerState,
        revision_id: str,
    ) -> dict[str, Any] | None:
        """Return one summary revision row for debug/test callers."""
        row = self.db.fetchone(
            "SELECT * FROM session_summary_revisions WHERE id = %s",
            (revision_id,),
        )
        return _summary_revision_from_row(row) if row else None

    def list_summary_revisions(
        self: _ManagerState,
        session_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return recent summary revisions for a session, newest first."""
        bounded_limit = max(1, min(int(limit), 100))
        rows = self.db.fetchall(
            """
            SELECT *
            FROM session_summary_revisions
            WHERE session_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (session_id, bounded_limit),
        )
        return [_summary_revision_from_row(row) for row in rows]

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
