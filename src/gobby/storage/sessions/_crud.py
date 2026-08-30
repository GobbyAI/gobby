"""CRUD mixin for session storage."""

from __future__ import annotations

import json
import uuid
from typing import Any, ClassVar, Protocol

from gobby.storage.hub.protocol import (
    HubDatabase,
    SessionRecoveryByProject,
    SessionRegistration,
    SessionSeqMutation,
)
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.session_models import Session
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)
from gobby.utils.datetime import utc_now

from ._constants import ensure_system_session, get_logger, system_session_id
from ._identity_crud import _SessionIdentityCRUDMixin
from ._identity_reconciliation import reconcile_session_identity
from ._lineage_guard import repair_self_parent_session, sanitize_parent_session_id
from ._registration import (
    manual_registration_title,
    require_registered_machine,
    require_valid_title_source,
)
from ._title_defaults import (
    PROVISIONAL_TITLE_SOURCE,
    format_provisional_session_title,
    project_name_for_session_title,
)
from ._update_sentinel import UNSET, UnsetType, is_set
from ._upsert import is_session_unique_conflict, update_existing_session


class _SessionCRUDHost(Protocol):
    db: HubDatabase
    _VALID_CHAT_MODES: ClassVar[set[str]]
    _VALID_TITLE_SOURCES: ClassVar[set[str]]

    def find_by_external_id(
        self,
        external_id: str,
        project_id: str | None,
        source: str,
        session_type: str | None = "terminal",
    ) -> Session | None: ...

    def find_by_external_id_any_project(
        self,
        external_id: str,
        source: str,
        session_type: str | None = "terminal",
    ) -> Session | None: ...

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def move_to_project(self, session_id: str, project_id: str) -> Session | None: ...

    def register(
        self,
        external_id: str,
        machine_id: str | None,
        source: str,
        project_id: str | None,
        title: str | None | UnsetType = UNSET,
        transcript_path: str | None | UnsetType = UNSET,
        git_branch: str | None | UnsetType = UNSET,
        parent_session_id: str | None | UnsetType = UNSET,
        agent_depth: int = 0,
        spawned_by_agent_id: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        workflow_name: str | None = None,
        session_type: str = "terminal",
        is_local: bool = False,
        sandbox_enabled: bool | None = None,
        sandbox_policy_hash: str | None = None,
        title_source: str | None | UnsetType = UNSET,
        workspace_path: str | None = None,
    ) -> Session: ...


class _SessionCRUDMixin(_SessionIdentityCRUDMixin):
    def register(
        self: _SessionCRUDHost,
        external_id: str,
        machine_id: str | None,
        source: str,
        project_id: str | None,
        title: str | None | UnsetType = UNSET,
        transcript_path: str | None | UnsetType = UNSET,
        git_branch: str | None | UnsetType = UNSET,
        parent_session_id: str | None | UnsetType = UNSET,
        agent_depth: int = 0,
        spawned_by_agent_id: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        workflow_name: str | None = None,
        session_type: str = "terminal",
        is_local: bool = False,
        sandbox_enabled: bool | None = None,
        sandbox_policy_hash: str | None = None,
        title_source: str | None | UnsetType = UNSET,
        workspace_path: str | None = None,
    ) -> Session:
        """Register a session or return the existing (external_id, source, project) row."""
        machine_id = require_local_machine_id(
            machine_id,
            resource_kind="session",
            resource_id=external_id,
        )
        now = utc_now()
        terminal_context_json = json.dumps(terminal_context) if terminal_context else None
        storage_project_id = project_id or PERSONAL_PROJECT_ID
        require_registered_machine(self.db, machine_id, seen_at=now)
        require_valid_title_source(title_source, self._VALID_TITLE_SOURCES)

        manual_title, manual_title_source = manual_registration_title(title, title_source)

        if is_set(parent_session_id) and parent_session_id == system_session_id():
            ensure_system_session(self.db)

        registration_lock = SessionRegistration(
            external_id=external_id,
            source=source,
            session_type=session_type,
        )

        change_event = "session_created"
        with self.db.transaction_immediate(registration_lock) as conn:
            # Deliberately project-agnostic, unlike idx_sessions_unique: register may
            # recover and move a same-identity session across projects, so foreign
            # ownership anywhere must block before that reuse path can run.
            existing_owners = conn.execute(
                """
                SELECT id, machine_id
                FROM sessions
                WHERE external_id = %s AND source = %s AND session_type = %s
                  AND status <> 'deleted'
                """,
                (external_id, source, session_type),
            ).fetchall()
            for owner in existing_owners:
                owner_machine_id = str(owner["machine_id"])
                if owner_machine_id != machine_id:
                    raise MachineOwnershipMismatchError(
                        resource_kind="session",
                        resource_id=str(owner["id"]),
                        owner_machine_id=owner_machine_id,
                        current_machine_id=machine_id,
                    )

            reconciliation = reconcile_session_identity(
                conn,
                external_id=external_id,
                source=source,
                project_id=storage_project_id,
                session_type=session_type,
            )
            if reconciliation.deleted_ids:
                get_logger().warning(
                    "Reconciled legacy duplicate sessions %s into %s",
                    reconciliation.deleted_ids,
                    reconciliation.canonical_id,
                )
            existing = self.find_by_external_id(
                external_id,
                project_id,
                source,
                session_type=session_type,
            )
            if existing is None and project_id:
                with self.db.transaction_immediate(SessionRecoveryByProject(project_id=project_id)):
                    existing = self.find_by_external_id_any_project(
                        external_id,
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
            elif existing is None:
                existing = self.find_by_external_id_any_project(
                    external_id,
                    source,
                    session_type=session_type,
                )

            if existing:
                registration_title = manual_title
                registration_title_source = manual_title_source
                existing_seq_num = existing.seq_num
                if (
                    not is_set(manual_title)
                    and existing_seq_num is not None
                    and not str(existing.title or "").strip()
                ):
                    registration_title = format_provisional_session_title(
                        project_name_for_session_title(conn, existing.project_id),
                        existing_seq_num,
                        existing.source,
                    )
                    registration_title_source = PROVISIONAL_TITLE_SOURCE
                if existing.parent_session_id == existing.id:
                    repair_self_parent_session(conn, session_id=existing.id, now=now)
                registration_parent_session_id = parent_session_id
                if is_set(parent_session_id) and parent_session_id == existing.id:
                    registration_parent_session_id = None
                if is_set(registration_parent_session_id):
                    sanitized_parent_session_id: str | None | UnsetType = (
                        sanitize_parent_session_id(
                            conn,
                            child_session_id=existing.id,
                            parent_session_id=registration_parent_session_id,
                            context="session registration",
                        )
                    )
                else:
                    sanitized_parent_session_id = UNSET
                session = update_existing_session(
                    self,
                    conn,
                    existing,
                    machine_id=machine_id,
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
                    workspace_path=workspace_path if workspace_path else UNSET,
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
                    parent_session_id=parent_session_id if is_set(parent_session_id) else None,
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
                    insert_title = manual_title if is_set(manual_title) else None
                    insert_title_source = (
                        manual_title_source if is_set(manual_title_source) else None
                    )
                    if insert_title is None:
                        insert_title = format_provisional_session_title(
                            project_name_for_session_title(conn, storage_project_id),
                            next_seq_num,
                            source,
                        )
                        insert_title_source = PROVISIONAL_TITLE_SOURCE
                    conn.execute(
                        """
                        INSERT INTO sessions (
                            id, external_id, machine_id, source, project_id, title, title_source,
                            transcript_path, git_branch, parent_session_id,
                            agent_depth, spawned_by_agent_id, terminal_context,
                            workflow_name, session_type, is_local, sandbox_enabled, sandbox_policy_hash,
                            workspace_path, workspace_generation,
                            status, seq_num,
                            had_edits, message_count, turn_count, tool_call_count, last_assistant_content
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, FALSE, 0, 0, 0, NULL)
                        """,
                        (
                            session_id,
                            external_id,
                            machine_id,
                            source,
                            storage_project_id,
                            insert_title,
                            insert_title_source,
                            transcript_path if is_set(transcript_path) else None,
                            git_branch if is_set(git_branch) else None,
                            sanitized_parent_session_id,
                            agent_depth,
                            spawned_by_agent_id,
                            terminal_context_json,
                            workflow_name,
                            session_type,
                            bool(is_local),
                            None if sandbox_enabled is None else bool(sandbox_enabled),
                            sandbox_policy_hash,
                            workspace_path,
                            1 if workspace_path else 0,
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
                        project_id,
                        source,
                        session_type=session_type,
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
                    if is_set(parent_session_id):
                        sanitized_parent_session_id = sanitize_parent_session_id(
                            conn,
                            child_session_id=conflicting.id,
                            parent_session_id=parent_session_id,
                            context="session registration",
                        )
                    else:
                        sanitized_parent_session_id = UNSET
                    registration_title = manual_title
                    registration_title_source = manual_title_source
                    conflicting_seq_num = conflicting.seq_num
                    if (
                        not is_set(manual_title)
                        and conflicting_seq_num is not None
                        and not str(conflicting.title or "").strip()
                    ):
                        registration_title = format_provisional_session_title(
                            project_name_for_session_title(conn, conflicting.project_id),
                            conflicting_seq_num,
                            conflicting.source,
                        )
                        registration_title_source = PROVISIONAL_TITLE_SOURCE
                    session = update_existing_session(
                        self,
                        conn,
                        conflicting,
                        machine_id=machine_id,
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
                        workspace_path=workspace_path if workspace_path else UNSET,
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
