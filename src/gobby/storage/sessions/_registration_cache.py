"""Registration/cache mixin for the unified session manager."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

from gobby.sessions.contested_expiry import ContestedExpiryCause
from gobby.storage.session_models import Session
from gobby.terminal_context import terminal_context_has_tmux_target
from gobby.utils.datetime import utc_now

from ._contested_expiry import record_contested_terminal_expiry
from ._registration_recovery import _RegistrationRecoveryMixin
from ._session_mapping_cache import (
    _get_session_mapping,
    _put_session_mapping,
    _SessionMappingState,
    invalidate_session_caches,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _ManagerState(_SessionMappingState, Protocol):
    db: HubDatabase
    logger: logging.Logger
    _session_metadata: dict[str, dict[str, Any]]
    _session_metadata_lock: Any

    def find_by_external_id(
        self,
        external_id: str,
        project_id: str | None,
        source: str,
        session_type: str | None = "terminal",
    ) -> Session | None: ...

    def get(self, session_id: str) -> Session | None: ...

    def update_status(self, session_id: str, status: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...


def _validated_session_mapping(
    state: _ManagerState,
    *,
    external_id: str,
    source: str,
    project_id: str | None,
    session_type: str,
) -> Session | None:
    session_id = _get_session_mapping(
        state,
        external_id=external_id,
        source=source,
        project_id=project_id,
        session_type=session_type,
    )
    if session_id is None:
        return None
    session = state.get(session_id)
    if session is not None and session_matches_registration(
        session,
        external_id=external_id,
        source=source,
        project_id=project_id,
        session_type=session_type,
    ):
        return session
    invalidate_session_caches(state, session_id)
    return None


def session_matches_registration(
    session: Session,
    *,
    external_id: str,
    source: str,
    project_id: str | None,
    session_type: str = "terminal",
) -> bool:
    """Whether a loaded row still backs the cached registration identity."""
    return (
        session.external_id == external_id
        and session.source == source
        and (project_id is None or session.project_id == project_id)
        and session.session_type == session_type
        and session.status not in {"expired", "deleted"}
    )


class _RegistrationCacheMixin(_RegistrationRecoveryMixin):
    def mark_session_expired(
        self: _ManagerState,
        session_id: str,
        *,
        cause: ContestedExpiryCause,
    ) -> bool:
        """
        Expire a session that SessionStart believes a newcomer has replaced.

        Both callers are guessing: neither has validated who owns the terminal,
        and ``revive_expired_terminal_session`` reverses the guess routinely.
        The cause is recorded on the session so the claim shields can tell this
        expiry from a final one and leave the owner's work alone (#20837).

        Args:
            session_id: Session ID to mark as expired
            cause: Which speculative SessionStart path expired it

        Returns:
            True if updated successfully, False otherwise
        """
        try:
            session = self.update_status(session_id, "expired")
            if session:
                if session.session_type == "terminal":
                    record_contested_terminal_expiry(self.db, session_id, cause)
                self.logger.debug("Session status updated: %s -> expired", session_id)
                return True

            self.logger.warning("Session not found for status update: %s", session_id)
            return False

        except Exception as e:
            self.logger.exception("Failed to update session status: %s", e)
            return False

    def lookup_session_id(
        self: _ManagerState,
        external_id: str,
        source: str,
        project_id: str | None,
        session_type: str = "terminal",
    ) -> str | None:
        """
        Look up session_id from database by full composite key.

        Args:
            external_id: External session identifier
            source: CLI source identifier (e.g., "claude", "codex", "grok")
            project_id: Project identifier

        Returns:
            session_id (database PK) or None if not found
        """
        try:
            cached_session = _validated_session_mapping(
                self,
                external_id=external_id,
                source=source,
                project_id=project_id,
                session_type=session_type,
            )
            if cached_session is not None:
                return cached_session.id

            session = self.find_by_external_id(external_id, project_id, source, session_type)
            if session and session.status not in {"expired", "deleted"}:
                session_id: str = session.id
                self.logger.debug(
                    "Looked up session_id %s for external_id %s",
                    session_id,
                    external_id,
                )
                _put_session_mapping(
                    self,
                    external_id=external_id,
                    source=source,
                    session_id=session_id,
                    project_id=session.project_id,
                    session_type=session.session_type,
                )
                return session_id

            return None

        except Exception as e:
            self.logger.debug(
                "Failed to lookup session_id from database: %s",
                e,
                exc_info=True,
            )
            return None

    def get_session_id(
        self: _ManagerState,
        external_id: str,
        source: str,
        project_id: str | None = None,
        session_type: str = "terminal",
    ) -> str | None:
        """
        Get a cached session ID for the supplied registration identity.

        Args:
            external_id: External session identifier
            source: CLI source identifier (e.g., "claude", "codex", "grok")
            project_id: Optional project scope.

        Returns:
            session_id or None if not cached
        """
        session = _validated_session_mapping(
            self,
            external_id=external_id,
            source=source,
            project_id=project_id,
            session_type=session_type,
        )
        return session.id if session is not None else None

    def get_cached_session(
        self: _ManagerState,
        external_id: str,
        source: str,
        project_id: str | None = None,
        session_type: str = "terminal",
    ) -> Session | None:
        """Return the row that validated the cached registration mapping, if any.

        Callers that need more than the id keep this row instead of reading it
        again: each read is a pooled hub round trip (#23063).
        """
        return _validated_session_mapping(
            self,
            external_id=external_id,
            source=source,
            project_id=project_id,
            session_type=session_type,
        )

    def cache_session_mapping(
        self: _ManagerState,
        external_id: str,
        source: str,
        session_id: str,
        project_id: str | None = None,
        session_type: str = "terminal",
    ) -> None:
        """
        Cache a fully scoped registration identity to session ID mapping.

        Args:
            external_id: External session identifier
            source: CLI source identifier (e.g., "claude", "agy", "codex")
            session_id: Database session ID
            project_id: Project identifier, when known.
        """
        _put_session_mapping(
            self,
            external_id=external_id,
            source=source,
            session_id=session_id,
            project_id=project_id,
            session_type=session_type,
        )

    def backfill_terminal_context(
        self: _ManagerState,
        session_id: str,
        terminal_context: dict[str, Any] | None,
        current: Session | None = None,
    ) -> tuple[Session | None, bool]:
        """Merge newly discovered terminal context into an existing session.

        ``current`` is this session's row when the caller already loaded it;
        the merge compares against it instead of reading the row again.

        Returns the updated session plus a flag indicating whether a tmux pane
        became available as part of the merge.
        """
        if current is None or current.id != session_id:
            current = self.get(session_id)
        if not terminal_context:
            return current, False
        if current is None:
            return None, False

        current_ctx = current.terminal_context or {}
        incoming = {key: value for key, value in terminal_context.items() if value is not None}
        if not incoming or all(current_ctx.get(key) == value for key, value in incoming.items()):
            return current, False

        had_tmux_target = terminal_context_has_tmux_target(current_ctx)
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET terminal_context = COALESCE(terminal_context, '{}'::jsonb) || %s::jsonb,
                    updated_at = %s
                WHERE id = %s
                """,
                (json.dumps(incoming), utc_now(), session_id),
            )
        updated = self.get(session_id)
        if updated is None:
            return current, False

        self._notify_session_change("session_updated", session_id)

        with self._session_metadata_lock:
            metadata = self._session_metadata.setdefault(session_id, {})
            metadata["terminal_context"] = updated.terminal_context

        has_tmux_target = terminal_context_has_tmux_target(updated.terminal_context)
        return updated, has_tmux_target and not had_tmux_target
