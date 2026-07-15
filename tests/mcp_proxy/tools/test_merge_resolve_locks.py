from __future__ import annotations

import pytest

from gobby.mcp_proxy.tools.merge_resolve_locks import (
    _MERGE_RESOLVE_LOCKS,
    release_resolve_lock,
    try_acquire_resolve_lock,
)


@pytest.mark.asyncio
async def test_release_evicts_resolution_lock() -> None:
    resolution_id = "test-resolution-lock-eviction"

    lock = await try_acquire_resolve_lock(resolution_id)

    assert lock is not None
    assert await try_acquire_resolve_lock(resolution_id) is None

    release_resolve_lock(resolution_id, lock)

    assert resolution_id not in _MERGE_RESOLVE_LOCKS
    reacquired = await try_acquire_resolve_lock(resolution_id)
    assert reacquired is not None
    assert reacquired is not lock
    release_resolve_lock(resolution_id, reacquired)
