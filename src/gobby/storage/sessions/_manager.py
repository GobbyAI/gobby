"""Local session storage manager."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from gobby.sessions.status_events import SessionStatusTransitionCallback
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)

from ._bootstrap import (
    SessionChangeCallback,
    TitleChangeCallback,
    _SessionBootstrapMixin,
)
from ._bulk_update import _BulkUpdateMixin
from ._constants import get_logger
from ._crud import _SessionCRUDMixin
from ._discovery import _DiscoveryMixin
from ._field_update import _FieldUpdateMixin
from ._identity_reconciliation import AmbiguousSessionIdentityError
from ._lifecycle_delegate import _LifecycleDelegateMixin
from ._query import _QueryMixin
from ._registration_cache import (
    SessionMappingKey,
    _put_session_mapping,
    _RegistrationCacheMixin,
)
from ._renumber import _RenumberMixin
from ._terminal import _TerminalMixin
from ._title_defaults import (
    MANUAL_TITLE_SOURCE,
    PROVISIONAL_TITLE_SOURCE,
    TASK_TITLE_SOURCE,
)
from ._transcript import _TranscriptMixin
from ._update_sentinel import UNSET, UnsetType
from ._usage import _UsageMixin

if TYPE_CHECKING:
    import logging

    from gobby.config.app import DaemonConfig


class SessionManager(
    _SessionBootstrapMixin,
    _SessionCRUDMixin,
    _DiscoveryMixin,
    _FieldUpdateMixin,
    _RegistrationCacheMixin,
    _BulkUpdateMixin,
    _QueryMixin,
    _RenumberMixin,
    _LifecycleDelegateMixin,
    _TranscriptMixin,
    _UsageMixin,
    _TerminalMixin,
):
    """Manager for local session storage."""

    db: HubDatabase
    _storage: SessionManager
    logger: logging.Logger
    _config: DaemonConfig | None
    _title_listeners: list[TitleChangeCallback]
    _session_change_listeners: list[SessionChangeCallback]
    _session_mapping: dict[SessionMappingKey, str]
    _session_mapping_timestamps: dict[SessionMappingKey, float]
    _session_mapping_lock: threading.Lock
    _session_metadata: dict[str, dict[str, Any]]
    _session_metadata_lock: threading.Lock

    _VALID_TITLE_SOURCES: ClassVar[set[str]] = {
        MANUAL_TITLE_SOURCE,
        PROVISIONAL_TITLE_SOURCE,
        TASK_TITLE_SOURCE,
    }

    def __init__(
        self,
        db: HubDatabase | None = None,
        *,
        session_storage: SessionManager | None = None,
        logger_instance: logging.Logger | None = None,
        config: DaemonConfig | None = None,
    ):
        """Initialize with either a database handle or a compatibility session_storage."""
        if session_storage is not None:
            resolved_db = session_storage.db
            storage = session_storage
        elif db is not None:
            resolved_db = db
            storage = self
        else:
            raise TypeError("SessionManager requires either db or session_storage")

        self.db = resolved_db
        self._storage: SessionManager = storage
        self.logger = logger_instance or get_logger()
        self._config = config
        self._title_listeners: list[TitleChangeCallback] = []
        self._session_change_listeners: list[SessionChangeCallback] = []
        self._status_transition_listeners: list[SessionStatusTransitionCallback] = []
        self._session_mapping: dict[SessionMappingKey, str] = {}
        self._session_mapping_timestamps: dict[SessionMappingKey, float] = {}
        self._session_mapping_lock = threading.Lock()
        self._session_metadata: dict[str, dict[str, Any]] = {}
        self._session_metadata_lock = threading.Lock()

    _VALID_CHAT_MODES: ClassVar[set[str]] = {"plan", "accept_edits", "normal", "bypass"}

    _VALID_SESSION_TYPES: ClassVar[set[str]] = {"terminal", "web_chat"}

    def _cache_registered_session(
        self,
        *,
        session_id: str,
        external_id: str,
        machine_id: str | None,
        source: str,
        project_id: str | None,
        session_type: str,
        parent_session_id: str | None,
        transcript_path: str | None,
        title: str | None,
        git_branch: str | None,
        workflow_name: str | None,
        agent_depth: int,
        is_local: bool,
        sandbox_enabled: bool | None,
    ) -> None:
        _put_session_mapping(
            self,
            external_id=external_id,
            source=source,
            session_id=session_id,
            project_id=project_id,
            session_type=session_type,
        )

        with self._session_metadata_lock:
            self._session_metadata[session_id] = {
                "external_id": external_id,
                "machine_id": machine_id,
                "source": source,
                "parent_session_id": parent_session_id,
                "transcript_path": transcript_path,
                "project_id": project_id,
                "title": title,
                "git_branch": git_branch,
                "workflow_name": workflow_name,
                "agent_depth": agent_depth,
                "is_local": is_local,
                "sandbox_enabled": sandbox_enabled,
            }

    def _recover_registered_session_after_failure(
        self,
        *,
        external_id: str,
        machine_id: str | None,
        source: str,
        project_id: str | None,
        parent_session_id: str | None | UnsetType,
        transcript_path: str | None,
        title: str | None,
        git_branch: str | None,
        workflow_name: str | None,
        agent_depth: int,
        is_local: bool,
        sandbox_enabled: bool | None,
    ) -> str:
        try:
            recovered = self.find_by_external_id(
                external_id=external_id,
                project_id=project_id,
                source=source,
                session_type="terminal",
            )
            if recovered is None:
                relaxed = self.find_active_by_external_id(
                    external_id,
                    source,
                    session_type="terminal",
                )
                if relaxed and (project_id is None or relaxed.project_id == project_id):
                    recovered = relaxed

            if recovered is None:
                return ""

            self._cache_registered_session(
                session_id=recovered.id,
                external_id=external_id,
                machine_id=machine_id,
                source=recovered.source,
                project_id=recovered.project_id,
                session_type=recovered.session_type,
                parent_session_id=recovered.parent_session_id,
                transcript_path=transcript_path,
                title=recovered.title,
                git_branch=git_branch,
                workflow_name=workflow_name,
                agent_depth=agent_depth,
                is_local=is_local,
                sandbox_enabled=sandbox_enabled,
            )
            return recovered.id
        except AmbiguousSessionIdentityError:
            raise
        except Exception as recovery_error:
            self.logger.debug(
                "Failed to recover persisted session after registration failure: %s",
                recovery_error,
                exc_info=True,
            )
            return ""

    def register_session(
        self,
        external_id: str,
        machine_id: str | None,
        source: str,
        project_id: str | None,
        parent_session_id: str | None | UnsetType = UNSET,
        transcript_path: str | None = None,
        title: str | None = None,
        git_branch: str | None = None,
        project_path: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        workflow_name: str | None = None,
        agent_depth: int = 0,
        is_local: bool = False,
        sandbox_enabled: bool | None = None,
    ) -> str:
        """
        Register new session with local storage.

        Returns an existing persisted session on recoverable storage failures.
        If no persisted session can be recovered, returns an empty string so
        callers do not inject an ephemeral wrapper ID into later hooks.
        """
        machine_id = require_local_machine_id(
            machine_id,
            resource_kind="session",
            resource_id=external_id,
        )
        working_dir = project_path or str(Path.cwd())

        if not git_branch:
            try:
                from gobby.utils.git import get_git_branch

                git_branch = get_git_branch(working_dir)
                if git_branch:
                    self.logger.debug("Extracted git_branch from project_path: %s", git_branch)
            except Exception as e:
                self.logger.debug("Could not extract git_branch: %s", e)

        try:
            session = self.register(
                external_id=external_id,
                machine_id=machine_id,
                source=source,
                project_id=project_id,
                title=title,
                transcript_path=transcript_path,
                git_branch=git_branch,
                parent_session_id=parent_session_id,
                terminal_context=terminal_context,
                workflow_name=workflow_name,
                agent_depth=agent_depth,
                is_local=is_local,
                sandbox_enabled=sandbox_enabled,
            )

            session_id = session.id

            self._cache_registered_session(
                session_id=session_id,
                external_id=session.external_id,
                machine_id=session.machine_id,
                source=session.source,
                project_id=session.project_id,
                session_type=session.session_type,
                parent_session_id=session.parent_session_id,
                transcript_path=transcript_path,
                title=session.title,
                git_branch=git_branch,
                workflow_name=workflow_name,
                agent_depth=agent_depth,
                is_local=is_local,
                sandbox_enabled=sandbox_enabled,
            )

            self.logger.debug(
                "Registered session %s (external_id=%s)",
                session_id,
                external_id,
            )
            return session_id

        except AmbiguousSessionIdentityError:
            raise
        except MachineOwnershipMismatchError:
            raise
        except Exception as e:
            recovered_session_id = self._recover_registered_session_after_failure(
                external_id=external_id,
                machine_id=machine_id,
                source=source,
                project_id=project_id,
                parent_session_id=parent_session_id,
                transcript_path=transcript_path,
                title=title,
                git_branch=git_branch,
                workflow_name=workflow_name,
                agent_depth=agent_depth,
                is_local=is_local,
                sandbox_enabled=sandbox_enabled,
            )
            if recovered_session_id:
                self.logger.warning(
                    "Session registration failed; reused existing session %s for "
                    "external_id=%s source=%s: %s",
                    recovered_session_id,
                    external_id,
                    source,
                    e,
                )
                return recovered_session_id

            self.logger.exception(
                "Failed to register session and no persisted session could be recovered: %s",
                e,
            )
            return ""

    def update_session_status(
        self,
        session_id: str,
        status: str,
        *,
        activity_confirmed: bool = False,
    ) -> bool:
        """
        Update session status and return a service-friendly success flag.

        This wraps update_status() for hooks, routes, and other callers that
        only need True/False plus logging rather than the updated Session row.
        Confirmed activity uses the guarded active/paused storage path.

        Returns:
            True if updated successfully, False otherwise
        """
        try:
            current = self.get(session_id)
            if (
                status == "handoff_ready"
                and current is not None
                and current.status == "expired"
                and current.session_type == "terminal"
            ):
                # A `/compact` (PRE_COMPACT) is fresh activity on an expired
                # terminal session: revive it through the ownership path first
                # so the terminal-transition guard does not reject the update.
                revived = self.revive_expired_terminal_session(session_id)
                if revived is not None and revived.status != "expired":
                    current = revived

            if activity_confirmed:
                session = self.update_status_from_activity(session_id, status)
            else:
                session = self.update_status(session_id, status)
            if session:
                self.logger.debug("Session status updated: %s -> %s", session_id, status)
                return True

            self.logger.warning("Session not found for status update: %s", session_id)
            return False

        except Exception as e:
            self.logger.exception("Failed to update session status: %s", e)
            return False
