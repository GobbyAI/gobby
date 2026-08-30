from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from gobby.sessions.machine_scope import is_local_machine_owner
from gobby.sessions.transcript_paths import MISSING_TRANSCRIPT_PATH, find_transcript_on_disk
from gobby.utils.datetime import parse_stored_datetime
from gobby.utils.machine_id import get_machine_id

if TYPE_CHECKING:
    from gobby.storage.session_models import Session


class WatchdogTranscriptResolver:
    """Resolve readable watchdog transcripts without mutating session state."""

    def __init__(self) -> None:
        self._path_cache: dict[tuple[str, str, str], str] = {}

    async def resolve(self, session: Session, *, run_id: str) -> str | None:
        local_machine_id = get_machine_id()
        if not is_local_machine_owner(session.machine_id, local_machine_id):
            return None

        source = session.source or ""
        external_id = session.external_id or ""
        cache_key = (run_id, source, external_id)

        stale_keys = [key for key in self._path_cache if key[0] == run_id and key != cache_key]
        for key in stale_keys:
            del self._path_cache[key]

        session_updated_at = parse_stored_datetime(session.updated_at)

        def is_current_file(path: str) -> bool:
            try:
                if not os.path.isfile(path):
                    return False
                return session_updated_at is None or os.path.getmtime(path) >= (
                    session_updated_at.timestamp()
                )
            except OSError:
                return False

        stored_path = session.transcript_path
        if (
            stored_path
            and stored_path != MISSING_TRANSCRIPT_PATH
            and await asyncio.to_thread(is_current_file, stored_path)
        ):
            return stored_path

        cached_path = self._path_cache.get(cache_key)
        if cached_path:
            cached_is_current = await asyncio.to_thread(is_current_file, cached_path)
            if cached_is_current:
                return cached_path
        self._path_cache.pop(cache_key, None)

        if not source or not external_id:
            return None
        discovered_path = await asyncio.to_thread(
            find_transcript_on_disk,
            source,
            external_id,
            owner_machine_id=session.machine_id,
            local_machine_id=local_machine_id,
            caller_context="recovery",
        )
        if not discovered_path:
            return None
        discovered_exists = await asyncio.to_thread(os.path.isfile, discovered_path)
        if not discovered_exists:
            return None

        self._path_cache[cache_key] = discovered_path
        return discovered_path

    def clear(self) -> None:
        self._path_cache.clear()

    def prune(self, active_run_ids: set[str]) -> None:
        self._path_cache = {
            key: transcript_path
            for key, transcript_path in self._path_cache.items()
            if key[0] in active_run_ids
        }

    def discard(self, run_id: str) -> None:
        self._path_cache = {
            key: transcript_path
            for key, transcript_path in self._path_cache.items()
            if key[0] != run_id
        }
