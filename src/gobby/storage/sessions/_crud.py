"""CRUD mixin for session storage."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

from gobby.storage.hub.protocol import (
    HubDatabase,
    SessionRecoveryByProject,
    SessionRegistration,
    WebChatSessionBootstrap,
)
from gobby.storage.session_models import Session

from ._constants import SYSTEM_SESSION_ID, ensure_system_session, get_logger
from ._lineage_guard import repair_self_parent_session, sanitize_parent_session_id
from ._upsert import is_session_unique_conflict, update_existing_session


class _SessionCRUDHost(Protocol):
    db: HubDatabase
    _VALID_CHAT_MODES: ClassVar[set[str]]

    def find_by_external_id(
        self,
        external_id: str,
        machine_id: str,
        project_id: str | None,
        source: str,
        session_type: str | None = None,
    ) -> Session | None: ...

    def find_by_external_id_any_project(
        self,
        external_id: str,
        machine_id: str,
        source: str,
        session_type: str | None = None,
    ) -> Session | None: ...

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def register(
        self,
        external_id: str,
        machine_id: str,
        source: str,
        project_id: str | None,
        title: str | None = None,
        transcript_path: str | None = None,
        git_branch: str | None = None,
        parent_session_id: str | None = None,
        agent_depth: int = 0,
        spawned_by_agent_id: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        workflow_name: str | None = None,
        session_type: str = "terminal",
        is_local: bool = False,
        sandbox_enabled: bool | None = None,
        sandbox_policy_hash: str | None = None,
    ) -> Session: ...


class _SessionCRUDMixin:
    def register(
        self: _SessionCRUDHost,
        external_id: str,
        machine_id: str,
        source: str,
        project_id: str | None,
        title: str | None = None,
        transcript_path: str | None = None,
        git_branch: str | None = None,
        parent_session_id: str | None = None,
        agent_depth: int = 0,
        spawned_by_agent_id: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        workflow_name: str | None = None,
        session_type: str = "terminal",
        is_local: bool = False,
        sandbox_enabled: bool | None = None,
        sandbox_policy_hash: str | None = None,
    ) -> Session:
        """
        Register a new session or return existing one.

        Looks up by (external_id, machine_id, project_id, source) to find if this
        exact session already exists (e.g., daemon restarted mid-session). If found,
        returns the existing session. Otherwise creates a new one.

        Args:
            external_id: External session identifier (e.g., Claude Code session ID)
            machine_id: Machine identifier
            source: CLI source (claude, gemini, qwen, codex, droid)
            project_id: Project ID (None if project context unavailable)
            title: Optional session title
            transcript_path: Path to transcript file
            git_branch: Git branch name
            parent_session_id: Parent session for handoff
            agent_depth: Nesting depth (0 = human-initiated, 1+ = agent-spawned)
            spawned_by_agent_id: ID of the agent that spawned this session

        Returns:
            Session instance
        """
        now = datetime.now(UTC).isoformat()
        terminal_context_json = json.dumps(terminal_context) if terminal_context else None

        if parent_session_id == SYSTEM_SESSION_ID:
            ensure_system_session(self.db)

        registration_lock = SessionRegistration(
            external_id=external_id,
            machine_id=machine_id,
            source=source,
            project_id=project_id,
            session_type=session_type,
        )

        change_event = "session_created"
        with self.db.transaction_immediate(registration_lock) as conn:
            existing = self.find_by_external_id(
                external_id,
                machine_id,
                project_id,
                source,
                session_type=session_type,
            )
            if existing is None and project_id:
                with self.db.transaction_immediate(SessionRecoveryByProject(project_id=project_id)):
                    existing = self.find_by_external_id_any_project(
                        external_id,
                        machine_id,
                        source,
                        session_type=session_type,
                    )
                    if existing and existing.project_id != project_id:
                        conn.execute(
                            "UPDATE sessions SET project_id = %s, updated_at = %s WHERE id = %s",
                            (project_id, now, existing.id),
                        )
                        get_logger().info(
                            "Recovered session %s: project_id %s -> %s",
                            existing.id,
                            existing.project_id,
                            project_id,
                        )
                        existing = self.get(existing.id)

            if existing:
                if existing.parent_session_id == existing.id:
                    repair_self_parent_session(conn, session_id=existing.id, now=now)
                sanitized_parent_session_id = sanitize_parent_session_id(
                    conn,
                    child_session_id=existing.id,
                    parent_session_id=parent_session_id,
                    context="session registration",
                )
                session = update_existing_session(
                    self,
                    conn,
                    existing,
                    title=title,
                    transcript_path=transcript_path,
                    git_branch=git_branch,
                    parent_session_id=sanitized_parent_session_id,
                    terminal_context_json=terminal_context_json,
                    workflow_name=workflow_name,
                    is_local=True if is_local else None,
                    sandbox_enabled=sandbox_enabled,
                    sandbox_policy_hash=sandbox_policy_hash,
                    now=now,
                )
                get_logger().debug(
                    "Reusing existing session %s for external_id=%s", existing.id, external_id
                )
                change_event = "session_updated"
            else:
                session_id = str(uuid.uuid4())
                sanitized_parent_session_id = sanitize_parent_session_id(
                    conn,
                    child_session_id=session_id,
                    parent_session_id=parent_session_id,
                    context="session registration",
                )
                max_seq_row = conn.execute(
                    "SELECT MAX(seq_num) as max_seq FROM sessions WHERE project_id = %s",
                    (project_id,),
                ).fetchone()
                next_seq_num = ((max_seq_row["max_seq"] if max_seq_row else None) or 0) + 1
                savepoint = (
                    conn.savepoint("session_register_insert")
                    if hasattr(conn, "savepoint")
                    else None
                )

                try:
                    conn.execute(
                        """
                        INSERT INTO sessions (
                            id, external_id, machine_id, source, project_id, title, title_source,
                            transcript_path, git_branch, parent_session_id,
                            agent_depth, spawned_by_agent_id, terminal_context,
                            workflow_name, session_type, is_local, sandbox_enabled, sandbox_policy_hash,
                            status, created_at, updated_at, seq_num,
                            had_edits, message_count, turn_count, tool_call_count, last_assistant_content
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, FALSE, 0, 0, 0, NULL)
                        """,
                        (
                            session_id,
                            external_id,
                            machine_id,
                            source,
                            project_id,
                            title,
                            transcript_path,
                            git_branch,
                            sanitized_parent_session_id,
                            agent_depth,
                            spawned_by_agent_id,
                            terminal_context_json,
                            workflow_name,
                            session_type,
                            bool(is_local),
                            None if sandbox_enabled is None else bool(sandbox_enabled),
                            sandbox_policy_hash,
                            now,
                            now,
                            next_seq_num,
                        ),
                    )
                except Exception as exc:
                    if savepoint is not None:
                        savepoint.rollback()
                        savepoint.release()
                    if not is_session_unique_conflict(exc):
                        raise
                    conflicting = self.find_by_external_id(
                        external_id,
                        machine_id,
                        project_id,
                        source,
                        session_type=session_type,
                    )
                    if conflicting is None:
                        conflicting = self.find_by_external_id(
                            external_id,
                            machine_id,
                            project_id,
                            source,
                            session_type=None,
                        )
                    if conflicting is None:
                        raise
                    get_logger().info(
                        "Recovered existing session %s after unique conflict for external_id=%s",
                        conflicting.id,
                        external_id,
                    )
                    if conflicting.parent_session_id == conflicting.id:
                        repair_self_parent_session(conn, session_id=conflicting.id, now=now)
                    sanitized_parent_session_id = sanitize_parent_session_id(
                        conn,
                        child_session_id=conflicting.id,
                        parent_session_id=parent_session_id,
                        context="session registration",
                    )
                    session = update_existing_session(
                        self,
                        conn,
                        conflicting,
                        title=title,
                        transcript_path=transcript_path,
                        git_branch=git_branch,
                        parent_session_id=sanitized_parent_session_id,
                        terminal_context_json=terminal_context_json,
                        workflow_name=workflow_name,
                        is_local=True if is_local else None,
                        sandbox_enabled=sandbox_enabled,
                        sandbox_policy_hash=sandbox_policy_hash,
                        now=now,
                    )
                    change_event = "session_updated"
                else:
                    if savepoint is not None:
                        savepoint.release()
                    get_logger().debug(
                        "Created new session %s for external_id=%s", session_id, external_id
                    )

                    created = self.get(session_id)
                    if created is None:
                        raise RuntimeError(f"Session {session_id} not found after creation")
                    session = created

        self._notify_session_change(change_event, session.id)
        return session

    def create_web_chat_session(
        self: _SessionCRUDHost,
        *,
        machine_id: str,
        project_id: str,
        source: str,
        title: str | None = None,
        model: str | None = None,
        is_local: bool = False,
        chat_mode: str | None = None,
        sandbox_enabled: bool,
        sandbox_policy_hash: str,
    ) -> Session:
        """Create a new web-chat session with a temporary runtime identity.

        The durable identity for web chat is the DB session ID. A temporary
        ``external_id`` is still required at row creation time and is later
        replaced with the provider-native runtime/session identifier when known.
        """
        if chat_mode is not None and chat_mode not in self._VALID_CHAT_MODES:
            raise ValueError(
                f"Invalid chat_mode {chat_mode!r}. Must be one of: {', '.join(sorted(self._VALID_CHAT_MODES))}"
            )

        bootstrap_external_id = f"web-chat-bootstrap:{uuid.uuid4()}"
        with self.db.transaction_immediate(
            WebChatSessionBootstrap(
                external_id=bootstrap_external_id,
                machine_id=machine_id,
                source=source,
                project_id=project_id,
                session_type="web_chat",
            )
        ):
            session = self.register(
                external_id=bootstrap_external_id,
                machine_id=machine_id,
                source=source,
                project_id=project_id,
                title=title,
                session_type="web_chat",
                is_local=is_local,
                sandbox_enabled=sandbox_enabled,
                sandbox_policy_hash=sandbox_policy_hash,
            )
            if model is None and chat_mode is None:
                return session

            now = datetime.now(UTC).isoformat()
            self.db.execute(
                """
                UPDATE sessions
                SET model = COALESCE(%s, model),
                    is_local = %s,
                    chat_mode = COALESCE(%s, chat_mode),
                    updated_at = %s
                WHERE id = %s
                """,
                (model, bool(is_local), chat_mode, now, session.id),
            )
            updated = self.get(session.id)
            if updated is None:
                raise RuntimeError(f"Web chat session {session.id} disappeared after update")
            return updated

    def get(self: _SessionCRUDHost, session_id: str) -> Session | None:
        """Get session by ID."""
        row = self.db.fetchone("SELECT * FROM sessions WHERE id = %s", (session_id,))
        return Session.from_row(row) if row else None

    def resolve_session_reference(
        self: _SessionCRUDHost, ref: str, project_id: str | None = None
    ) -> str:
        """Resolve a session reference to a UUID.

        Delegates to standalone resolve_session_reference() function.
        See session_resolution.py for full documentation.
        """
        from gobby.storage.session_resolution import (
            resolve_session_reference as _resolve,
        )

        return _resolve(self.db, ref, project_id)

    def touch(self: _SessionCRUDHost, session_id: str) -> None:
        """Refresh updated_at without changing any other fields.

        Used by the liveness monitor to keep tmux-backed sessions warm
        so the 30-minute pause timeout doesn't fire while the tmux pane
        is still alive.
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction():
            self.db.execute(
                "UPDATE sessions SET updated_at = %s WHERE id = %s",
                (now, session_id),
            )

    def delete(self: _SessionCRUDHost, session_id: str) -> bool:
        """Delete session by ID."""
        with self.db.transaction():
            cursor = self.db.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            self._notify_session_change("session_deleted", session_id)
        return deleted
