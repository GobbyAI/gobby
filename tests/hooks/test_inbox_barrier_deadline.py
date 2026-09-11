"""The replay barrier deadline bounds lock contention and slow ingress."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from gobby.hooks.inbox import _get_hook_inbox_drain_lock, drain_hook_inbox_barrier


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_on", ["lock", "post"])
async def test_barrier_deadline_retains_unresolved_envelope(
    tmp_path: Path, blocked_on: str
) -> None:
    app = FastAPI()
    pending = tmp_path / "pending.json"
    pending.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enqueued_at": "2026-04-16T12:00:00Z",
                "critical": False,
                "response_capability": "hook-response.v1",
                "hook_type": "session-start",
                "input_data": {"terminal_context": {"gobby_agent_run_id": "run-1"}},
                "source": "claude",
            }
        )
    )
    lock = _get_hook_inbox_drain_lock(app)
    if blocked_on == "lock":
        await lock.acquire()

    async def stalled_post(*args: object, **kwargs: object) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200)

    try:
        with (
            patch("gobby.hooks.inbox.read_local_api_token", return_value="test-token"),
            patch("gobby.hooks.inbox._post_envelope", stalled_post),
        ):
            result = await asyncio.wait_for(
                drain_hook_inbox_barrier(app, tmp_path, timeout_seconds=0.01), timeout=1
            )
        assert result.timed_out is True
        assert result.unresolved_run_ids == ("run-1",)
        assert pending.exists()
        assert lock.locked() is (blocked_on == "lock")
    finally:
        if blocked_on == "lock":
            lock.release()

    with (
        patch("gobby.hooks.inbox.read_local_api_token", return_value="test-token"),
        patch("gobby.hooks.inbox._post_envelope", new=AsyncMock(return_value=httpx.Response(200))),
    ):
        retried = await drain_hook_inbox_barrier(app, tmp_path, timeout_seconds=1)
    assert not retried.timed_out
    assert retried.replayed == 1
    assert not pending.exists()


@pytest.mark.asyncio
async def test_barrier_preserves_caller_cancellation(tmp_path: Path) -> None:
    app = FastAPI()
    lock = _get_hook_inbox_drain_lock(app)
    async with lock:
        async with asyncio.TaskGroup() as group:
            barrier = group.create_task(drain_hook_inbox_barrier(app, tmp_path))
            await asyncio.sleep(0)
            barrier.cancel()
        assert barrier.cancelled()
        assert lock.locked()


@pytest.mark.asyncio
async def test_barrier_does_not_mask_unrelated_timeout(tmp_path: Path) -> None:
    with patch(
        "gobby.hooks.inbox._drain_hook_inbox_once_locked",
        new=AsyncMock(side_effect=TimeoutError("ingress failure")),
    ):
        with pytest.raises(TimeoutError, match="ingress failure"):
            await drain_hook_inbox_barrier(FastAPI(), tmp_path)
