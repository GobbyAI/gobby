"""Registration/cache mixin for the unified session manager."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

from gobby.storage.session_models import Session
from gobby.terminal_context import terminal_context_has_tmux_target
from gobby.utils.datetime import utc_now

from ._registration_recovery import _RegistrationRecoveryMixin

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

SessionMappingKey = tuple[str, str, str | None, str | None]
_SESSION_MAPPING_TTL_SECONDS = 60 * 60
_SESSION_MAPPING_MAX_ENTRIES = 4096


class _SessionMappingState(Protocol):
    _session_mapping: dict[SessionMappingKey, str]
    _session_mapping_timestamps: dict[SessionMappingKey, float]
    _session_mapping_lock: Any


class _ManagerState(_SessionMappingState, Protocol):
    db: HubDatabase
    logger: logging.Logger
    _session_metadata: dict[str, dict[str, Any]]
    _session_metadata_lock: Any

    def find_by_external_id(
        self,
        external_id: str,
        machine_id: str,
        project_id: str | None,
        source: str,
        session_type: str | None = None,
    ) -> Session | None: ...

    def find_by_external_id_all_sources(
        self,
        external_id: str,
        machine_id: str,
        project_id: str | None,
        session_type: str | None = None,
    ) -> list[Session]: ...

    def find_parent(
        self,
        machine_id: str,
        project_id: str,
        source: str | None = None,
        status: str = "handoff_ready",
        max_age_minutes: int = 10,
    ) -> Session | None: ...

    def get(self, session_id: str) -> Session | None: ...

    def update_status(self, session_id: str, status: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def cache_session_mapping(
        self,
        external_id: str,
        source: str,
        session_id: str,
        machine_id: str | None = None,
        project_id: str | None = None,
    ) -> None: ...


def _session_mapping_key(
    external_id: str,
    source: str,
    machine_id: str | None,
    project_id: str | None,
) -> SessionMappingKey:
    return (external_id, source, machine_id, project_id)


def _purge_session_mapping(state: _SessionMappingState, now: float) -> None:
    stale_keys = [
        key
        for key, cached_at in state._session_mapping_timestamps.items()
        if key not in state._session_mapping or now - cached_at >= _SESSION_MAPPING_TTL_SECONDS
    ]
    for key in stale_keys:
        state._session_mapping.pop(key, None)
        state._session_mapping_timestamps.pop(key, None)


def _get_session_mapping(
    state: _SessionMappingState,
    *,
    external_id: str,
    source: str,
    machine_id: str | None,
    project_id: str | None,
) -> str | None:
    now = time.monotonic()
    with state._session_mapping_lock:
        _purge_session_mapping(state, now)
        if machine_id is not None or project_id is not None:
            key = _session_mapping_key(external_id, source, machine_id, project_id)
            session_id = state._session_mapping.get(key)
            if session_id is not None:
                state._session_mapping_timestamps[key] = now
            return session_id

        matches = {
            session_id
            for (cached_external, cached_source, _, _), session_id in state._session_mapping.items()
            if cached_external == external_id and cached_source == source
        }
        if len(matches) != 1:
            return None
        session_id = matches.pop()
        for key, cached_session_id in state._session_mapping.items():
            if cached_session_id == session_id and key[:2] == (external_id, source):
                state._session_mapping_timestamps[key] = now
        return session_id


def _put_session_mapping(
    state: _SessionMappingState,
    *,
    external_id: str,
    source: str,
    session_id: str,
    machine_id: str | None,
    project_id: str | None,
) -> None:
    now = time.monotonic()
    key = _session_mapping_key(external_id, source, machine_id, project_id)
    with state._session_mapping_lock:
        _purge_session_mapping(state, now)
        if key not in state._session_mapping and len(state._session_mapping) >= (
            _SESSION_MAPPING_MAX_ENTRIES
        ):
            oldest_key = min(
                state._session_mapping,
                key=lambda cached_key: state._session_mapping_timestamps.get(cached_key, 0.0),
            )
            state._session_mapping.pop(oldest_key, None)
            state._session_mapping_timestamps.pop(oldest_key, None)
        state._session_mapping[key] = session_id
        state._session_mapping_timestamps[key] = now


def invalidate_session_caches(state: Any, session_id: str | None = None) -> None:
    """Remove cached registration data for one session, or clear all entries."""
    with state._session_mapping_lock:
        if session_id is None:
            state._session_mapping.clear()
            state._session_mapping_timestamps.clear()
        else:
            keys = [
                key
                for key, cached_session_id in state._session_mapping.items()
                if cached_session_id == session_id
            ]
            for key in keys:
                state._session_mapping.pop(key, None)
                state._session_mapping_timestamps.pop(key, None)
    with state._session_metadata_lock:
        if session_id is None:
            state._session_metadata.clear()
        else:
            state._session_metadata.pop(session_id, None)


class _RegistrationCacheMixin(_RegistrationRecoveryMixin):
    def mark_session_expired(self: _ManagerState, session_id: str) -> bool:
        """
        Mark a session as 'expired' after successful handoff.

        Args:
            session_id: Session ID to mark as expired

        Returns:
            True if updated successfully, False otherwise
        """
        try:
            session = self.update_status(session_id, "expired")
            if session:
                self.logger.debug("Session status updated: %s -> expired", session_id)
                return True

            self.logger.warning("Session not found for status update: %s", session_id)
            return False

        except Exception as e:
            self.logger.error("Failed to update session status: %s", e, exc_info=True)
            return False

    def lookup_session_id(
        self: _ManagerState,
        external_id: str,
        source: str,
        machine_id: str,
        project_id: str | None,
    ) -> str | None:
        """
        Look up session_id from database by full composite key.

        Args:
            external_id: External session identifier
            source: CLI source identifier (e.g., "claude", "qwen", "codex")
            machine_id: Machine identifier
            project_id: Project identifier

        Returns:
            session_id (database PK) or None if not found
        """
        try:
            cached_session_id = _get_session_mapping(
                self,
                external_id=external_id,
                source=source,
                machine_id=machine_id,
                project_id=project_id,
            )
            if cached_session_id is not None:
                return cached_session_id

            session = self.find_by_external_id(external_id, machine_id, project_id, source)
            if session:
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
                    machine_id=machine_id,
                    project_id=project_id,
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
        machine_id: str | None = None,
        project_id: str | None = None,
    ) -> str | None:
        """
        Get a cached session ID for the supplied registration identity.

        Args:
            external_id: External session identifier
            source: CLI source identifier (e.g., "claude", "qwen", "codex")
            machine_id: Optional machine scope. When omitted with project_id,
                lookup succeeds only if the external/source mapping is unique.
            project_id: Optional project scope.

        Returns:
            session_id or None if not cached
        """
        return _get_session_mapping(
            self,
            external_id=external_id,
            source=source,
            machine_id=machine_id,
            project_id=project_id,
        )

    def cache_session_mapping(
        self: _ManagerState,
        external_id: str,
        source: str,
        session_id: str,
        machine_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """
        Cache a fully scoped registration identity to session ID mapping.

        Args:
            external_id: External session identifier
            source: CLI source identifier (e.g., "claude", "agy", "codex")
            session_id: Database session ID
            machine_id: Machine identifier, when known.
            project_id: Project identifier, when known.
        """
        _put_session_mapping(
            self,
            external_id=external_id,
            source=source,
            session_id=session_id,
            machine_id=machine_id,
            project_id=project_id,
        )

    def backfill_terminal_context(
        self: _ManagerState,
        session_id: str,
        terminal_context: dict[str, Any] | None,
    ) -> tuple[Session | None, bool]:
        """Merge newly discovered terminal context into an existing session.

        Returns the updated session plus a flag indicating whether a tmux pane
        became available as part of the merge.
        """
        if not terminal_context:
            session = self.get(session_id)
            return session, False

        current = self.get(session_id)
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
