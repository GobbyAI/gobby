"""Per-resolution concurrency guard for AI merge conflict resolution."""

from __future__ import annotations

import asyncio

_MERGE_RESOLVE_LOCKS: dict[str, asyncio.Lock] = {}


async def try_acquire_resolve_lock(resolution_id: str) -> asyncio.Lock | None:
    lock = _MERGE_RESOLVE_LOCKS.setdefault(resolution_id, asyncio.Lock())
    if lock.locked():
        return None
    await lock.acquire()
    return lock
