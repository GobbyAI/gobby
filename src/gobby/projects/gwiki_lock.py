"""Cross-process drain barrier compatible with gwiki's project advisory lock."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any


def gwiki_project_lock_key(project_id: str) -> int:
    """Return the signed PostgreSQL advisory key used by gwiki ingest."""
    digest = hashlib.sha256(b"gwiki:project:" + project_id.encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class GwikiProjectDrainTimeout(TimeoutError):
    """Raised when an external gwiki writer does not drain within the bound."""


class GwikiProjectDrainBarrier:
    """Acquire gwiki's exact session lock on a dedicated database connection."""

    def __init__(
        self,
        db: Any,
        *,
        connection_factory: Callable[[], Any] | None = None,
        poll_seconds: float = 0.05,
    ) -> None:
        factory = connection_factory or getattr(db, "_open_advisory_lock_connection", None)
        if not callable(factory):
            raise TypeError("Database does not support dedicated advisory-lock connections")
        self._connection_factory = factory
        self._poll_seconds = poll_seconds

    @asynccontextmanager
    async def drain(self, project_id: str, *, timeout: float) -> AsyncIterator[None]:
        key = gwiki_project_lock_key(project_id)
        connection = await asyncio.to_thread(self._connection_factory)
        acquired = False
        deadline = time.monotonic() + timeout
        try:
            while not acquired:
                acquired = await asyncio.to_thread(_try_lock, connection, key)
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    raise GwikiProjectDrainTimeout(
                        f"Timed out draining gwiki writers for project {project_id}"
                    )
                await asyncio.sleep(min(self._poll_seconds, max(deadline - time.monotonic(), 0)))
            yield
        finally:
            if acquired:
                await asyncio.to_thread(_unlock, connection, key)
            await asyncio.to_thread(connection.close)


def _try_lock(connection: Any, key: int) -> bool:
    row = connection.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()
    if isinstance(row, dict):
        return bool(row["acquired"])
    return bool(row[0])


def _unlock(connection: Any, key: int) -> None:
    connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
