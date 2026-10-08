"""In-process cache of external session identities to session ids."""

from __future__ import annotations

import time
from typing import Any, Protocol

SessionMappingKey = tuple[str, str, str | None, str]
_SESSION_MAPPING_TTL_SECONDS = 60 * 60
_SESSION_MAPPING_MAX_ENTRIES = 4096


class _SessionMappingState(Protocol):
    _session_mapping: dict[SessionMappingKey, str]
    _session_mapping_timestamps: dict[SessionMappingKey, float]
    _session_mapping_lock: Any


def _session_mapping_key(
    external_id: str,
    source: str,
    project_id: str | None,
    session_type: str,
) -> SessionMappingKey:
    return (external_id, source, project_id, session_type)


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
    project_id: str | None,
    session_type: str,
) -> str | None:
    now = time.monotonic()
    with state._session_mapping_lock:
        _purge_session_mapping(state, now)
        if project_id is not None:
            key = _session_mapping_key(external_id, source, project_id, session_type)
            session_id = state._session_mapping.get(key)
            if session_id is not None:
                state._session_mapping_timestamps[key] = now
            return session_id

        matches = {
            session_id
            for (
                cached_external,
                cached_source,
                _,
                cached_session_type,
            ), session_id in state._session_mapping.items()
            if cached_external == external_id
            and cached_source == source
            and cached_session_type == session_type
        }
        if len(matches) != 1:
            return None
        session_id = matches.pop()
        for key, cached_session_id in state._session_mapping.items():
            if (
                cached_session_id == session_id
                and key[:2] == (external_id, source)
                and key[3] == session_type
            ):
                state._session_mapping_timestamps[key] = now
        return session_id


def _put_session_mapping(
    state: _SessionMappingState,
    *,
    external_id: str,
    source: str,
    session_id: str,
    project_id: str | None,
    session_type: str,
) -> None:
    now = time.monotonic()
    key = _session_mapping_key(external_id, source, project_id, session_type)
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
