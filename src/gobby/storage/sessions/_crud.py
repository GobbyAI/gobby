"""CRUD mixin for session storage."""

from __future__ import annotations

import json
import uuid
from typing import Any, ClassVar, Protocol

import psycopg

from gobby.storage.hub.protocol import (
    HubDatabase,
    SessionRecoveryByProject,
    SessionRegistration,
    SessionSeqMutation,
)
from gobby.storage.machines import LocalMachineManager
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.session_models import Session
from gobby.storage.session_resolution import is_session_uuid
from gobby.utils.datetime import utc_now

from ._constants import SYSTEM_SESSION_ID, ensure_system_session, get_logger
from ._lineage_guard import repair_self_parent_session, sanitize_parent_session_id
from ._title_defaults import PROVISIONAL_TITLE_SOURCE, format_provisional_session_title
from ._upsert import is_session_unique_conflict, update_existing_session
from ._web_chat_crud import _SessionWebChatCRUDMixin


class _SessionCRUDHost(Protocol):
    db: HubDatabase
    _VALID_CHAT_MODES: ClassVar[set[str]]
    _VALID_TITLE_SOURCES: ClassVar[set[str]]

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

    def move_to_project(self, session_id: str, project_id: str) -> Session | None: ...

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
        title_source: str | None = None,
    ) -> Session: ...


class _SessionCRUDMixin(_SessionWebChatCRUDMixin):
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
        title_source: str | None = None,
    ) -> Session:
        """
        Register a new session or return existing one.

        Looks up by (external_id, machine_id, project_id, source) to find if this exact
        session already exists (e.g., daemon restarted mid-session). If found, returns
        the existing session. Otherwise creates a new one.

        Args:
            external_id: External session identifier (e.g., Claude Code session ID)
            machine_id: Machine identifier
            source: CLI source (claude, qwen, codex, droid)
            project_id: Project ID (None if project context unavailable)
            title: Optional session title
            title_source: Optional provenance for an explicit session title
            transcript_path: Path to transcript file
            git_branch: Git branch name
            parent_session_id: Parent session for handoff
            agent_depth: Nesting depth (0 = human-initiated, 1+ = agent-spawned)
            spawned_by_agent_id: ID of the agent that spawned this session

        Returns:
            Session instance
        """
        now = utc_now()
        terminal_context_json = json.dumps(terminal_context) if terminal_context else None
        storage_project_id = project_id or PERSONAL_PROJECT_ID
        try:
            LocalMachineManager(self.db).upsert_seen(machine_id, seen_at=now)
        except psycopg.Error as exc:
            get_logger().warning(
                "Failed to refresh machine registry during session registration",
                extra={"machine_id": machine_id, "error": str(exc)},
                exc_info=True,
            )

        if title_source is not None and title_source not in self._VALID_TITLE_SOURCES:
            sources = ", ".join(sorted(self._VALID_TITLE_SOURCES))
            raise ValueError(f"Invalid title_source {title_source!r}. Must be one of: {sources}")

        if parent_session_id == SYSTEM_SESSION_ID:
            ensure_system_session(self.db)

        registration_lock = SessionRegistration(
            external_id=external_id,
            machine_id=machine_id,
            source=source,
            project_id=storage_project_id,
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
                        previous_project_id = existing.project_id
                        existing = self.move_to_project(existing.id, project_id)
                        if existing is None:
                            raise RuntimeError("Recovered session disappeared during project move")
                        get_logger().info(
                            "Recovered session %s: project_id %s -> %s",
                            existing.id,
                            previous_project_id,
                            project_id,
                        )

            if existing:
                registration_title = title
                registration_title_source = title_source if title is not None else None
                existing_seq_num = existing.seq_num
                if (
                    title is None
                    and existing_seq_num is not None
                    and not str(existing.title or "").strip()
                ):
                    registration_title = format_provisional_session_title(
                        existing_seq_num,
                        source,
                    )
                    registration_title_source = PROVISIONAL_TITLE_SOURCE
                if existing.parent_session_id == existing.id:
                    repair_self_parent_session(conn, session_id=existing.id, now=now)
                registration_parent_session_id = (
                    None if parent_session_id == existing.id else parent_session_id
                )
                sanitized_parent_session_id = sanitize_parent_session_id(
                    conn,
                    child_session_id=existing.id,
                    parent_session_id=registration_parent_session_id,
                    context="session registration",
                )
                session = update_existing_session(
                    self,
                    conn,
                    existing,
                    title=registration_title,
                    title_source=registration_title_source,
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
                conn.acquire_additional_lock(SessionSeqMutation(project_id=storage_project_id))
                max_seq_row = conn.execute(
                    "SELECT MAX(seq_num) as max_seq FROM sessions WHERE project_id = %s",
                    (storage_project_id,),
                ).fetchone()
                next_seq_num = ((max_seq_row["max_seq"] if max_seq_row else None) or 0) + 1
                savepoint = (
                    conn.savepoint("session_register_insert")
                    if hasattr(conn, "savepoint")
                    else None
                )

                try:
                    insert_title = title
                    insert_title_source = title_source if title is not None else None
                    if insert_title is None:
                        insert_title = format_provisional_session_title(next_seq_num, source)
                        insert_title_source = PROVISIONAL_TITLE_SOURCE
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
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, FALSE, 0, 0, 0, NULL)
                        """,
                        (
                            session_id,
                            external_id,
                            machine_id,
                            source,
                            storage_project_id,
                            insert_title,
                            insert_title_source,
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
                    registration_title = title
                    registration_title_source = title_source if title is not None else None
                    conflicting_seq_num = conflicting.seq_num
                    if (
                        title is None
                        and conflicting_seq_num is not None
                        and not str(conflicting.title or "").strip()
                    ):
                        registration_title = format_provisional_session_title(
                            conflicting_seq_num,
                            source,
                        )
                        registration_title_source = PROVISIONAL_TITLE_SOURCE
                    session = update_existing_session(
                        self,
                        conn,
                        conflicting,
                        title=registration_title,
                        title_source=registration_title_source,
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

    def move_to_project(
        self: _SessionCRUDHost,
        session_id: str,
        project_id: str,
    ) -> Session | None:
        """Move a session and allocate its sequence number in the destination project."""
        with self.db.transaction_immediate(SessionSeqMutation(project_id=project_id)) as conn:
            existing = self.get(session_id)
            if existing is None:
                return None
            if existing.project_id == project_id:
                return existing

            max_seq_row = conn.execute(
                "SELECT MAX(seq_num) AS max_seq FROM sessions WHERE project_id = %s",
                (project_id,),
            ).fetchone()
            next_seq_num = ((max_seq_row["max_seq"] if max_seq_row else None) or 0) + 1
            conn.execute(
                """
                UPDATE sessions
                SET project_id = %s, seq_num = %s, updated_at = %s
                WHERE id = %s
                """,
                (project_id, next_seq_num, utc_now(), existing.id),
            )
            moved = self.get(existing.id)
            if moved is None:
                raise RuntimeError(f"Session {existing.id} not found after project move")
            return moved

    def get(self: _SessionCRUDHost, session_id: str) -> Session | None:
        """Get session by ID."""
        if not is_session_uuid(session_id):
            return None
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
        now = utc_now()
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
