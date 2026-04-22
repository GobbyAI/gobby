"""Local session storage manager."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from gobby.storage.database import DatabaseProtocol

from ._bootstrap import _SessionBootstrapMixin
from ._bulk_update import _BulkUpdateMixin
from ._constants import get_logger
from ._crud import _SessionCRUDMixin
from ._discovery import _DiscoveryMixin
from ._field_update import _FieldUpdateMixin
from ._lifecycle_delegate import _LifecycleDelegateMixin
from ._query import _QueryMixin
from ._registration_cache import _RegistrationCacheMixin
from ._terminal import _TerminalMixin
from ._transcript import _TranscriptMixin
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
    _LifecycleDelegateMixin,
    _TranscriptMixin,
    _UsageMixin,
    _TerminalMixin,
):
    """Manager for local session storage."""

    db: DatabaseProtocol
    _storage: SessionManager
    logger: logging.Logger
    _config: DaemonConfig | None
    _title_listeners: list[Callable[[str, str], None]]
    _session_mapping: dict[tuple[str, str], str]
    _session_mapping_lock: Any
    _session_metadata: dict[str, dict[str, Any]]
    _session_metadata_lock: Any

    _VALID_TITLE_SOURCES: ClassVar[set[str]] = {"heuristic", "llm", "manual"}

    def __init__(
        self,
        db: DatabaseProtocol | None = None,
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
        self._title_listeners: list[Callable[[str, str], None]] = []
        self._session_mapping: dict[tuple[str, str], str] = {}
        self._session_mapping_lock = threading.Lock()
        self._session_metadata: dict[str, dict[str, Any]] = {}
        self._session_metadata_lock = threading.Lock()

    _VALID_CHAT_MODES: ClassVar[set[str]] = {"plan", "accept_edits", "normal", "bypass"}

    _VALID_SESSION_TYPES: ClassVar[set[str]] = {"terminal", "web_chat"}

    def register_session(
        self,
        external_id: str,
        machine_id: str,
        source: str,
        project_id: str | None,
        parent_session_id: str | None = None,
        transcript_path: str | None = None,
        title: str | None = None,
        git_branch: str | None = None,
        project_path: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        workflow_name: str | None = None,
        agent_depth: int = 0,
        sandbox_enabled: bool | None = None,
    ) -> str:
        """
        Register new session with local storage.

        Returns a temporary UUID on failure so hooks can continue without
        persisting or caching an ephemeral row.
        """
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
                sandbox_enabled=sandbox_enabled,
            )

            session_id = session.id

            with self._session_mapping_lock:
                self._session_mapping[(external_id, source)] = session_id

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
                    "sandbox_enabled": sandbox_enabled,
                }

            self.logger.debug(
                "Registered session %s (external_id=%s)",
                session_id,
                external_id,
            )
            return session_id

        except Exception as e:
            self.logger.error("Failed to register session: %s", e, exc_info=True)
            return str(uuid.uuid4())

    def update_session_status(
        self,
        session_id: str,
        status: str,
    ) -> bool:
        """
        Update session status in database.

        Returns:
            True if updated successfully, False otherwise
        """
        try:
            session = self.update_status(session_id, status)
            if session:
                self.logger.debug("Session status updated: %s -> %s", session_id, status)
                return True

            self.logger.warning("Session not found for status update: %s", session_id)
            return False

        except Exception as e:
            self.logger.error("Failed to update session status: %s", e, exc_info=True)
            return False
