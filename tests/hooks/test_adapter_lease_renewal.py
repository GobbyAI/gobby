"""Lease renewal must leave the daemon loop available while the filesystem waits."""

import asyncio
import threading

import pytest

from gobby.hooks import adapter_execution


@pytest.mark.parametrize("outcome", ["renewed", "lost", "error", "cancelled"])
async def test_lease_renewal_does_not_block_loop(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    calls: list[tuple[str, str]] = []

    def renew(envelope_id: str, owner_token: str) -> bool:
        assert threading.get_ident() != loop_thread
        calls.append((envelope_id, owner_token))
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(2), "event loop could not release filesystem worker"
            if outcome == "error":
                raise OSError("lease storage unavailable")
            return outcome == "renewed" and len(calls) == 1
        finally:
            finished.set()

    monkeypatch.setattr(adapter_execution, "ENVELOPE_PROCESSING_LEASE_TTL_SECONDS", 0)
    monkeypatch.setattr(adapter_execution, "renew_envelope_processing_lease", renew)
    task = asyncio.create_task(adapter_execution._renew_envelope_lease("envelope", "owner"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        if outcome == "cancelled":
            task.cancel()
        release.set()
        if outcome == "error":
            with pytest.raises(OSError, match="lease storage unavailable"):
                await task
        elif outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        expected_renewals = 2 if outcome == "renewed" else 1
        assert calls == [("envelope", "owner")] * expected_renewals
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 2)
