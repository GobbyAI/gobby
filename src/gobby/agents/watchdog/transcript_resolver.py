from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from gobby.sessions.transcript_paths import _find_transcript_on_disk

if TYPE_CHECKING:
    from gobby.storage.session_models import Session


class WatchdogTranscriptResolver:
    """Resolve readable watchdog transcripts without mutating session state."""

    def __init__(self) -> None:
        self._path_cache: dict[tuple[str, str, str], str] = {}

    async def resolve(self, session: Session, *, run_id: str) -> str | None:
        source = session.source or ""
        external_id = session.external_id or ""
        cache_key = (run_id, source, external_id)

        stale_keys = [key for key in self._path_cache if key[0] == run_id and key != cache_key]
        for key in stale_keys:
            del self._path_cache[key]

        stored_path = session.transcript_path
        if stored_path and stored_path != "missing_transcript" and os.path.isfile(stored_path):
            return stored_path

        cached_path = self._path_cache.get(cache_key)
        if cached_path and os.path.isfile(cached_path):
            return cached_path
        self._path_cache.pop(cache_key, None)

        if not source or not external_id:
            return None
        discovered_path = await asyncio.to_thread(
            _find_transcript_on_disk,
            source,
            external_id,
        )
        if not discovered_path or not os.path.isfile(discovered_path):
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
