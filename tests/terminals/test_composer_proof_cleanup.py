"""Cancellation must finish before an owned provider may be terminated."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gobby.events.wake import WakeDispatcher
from tests.e2e.composer_proof import ProofRefused
from tests.e2e.composer_proof_cleanup import CleanupControl, cancel_proof_retries, quiesce_proof

pytestmark = pytest.mark.unit


async def pending_retry() -> None:
    await asyncio.Event().wait()


async def test_cancellation_awaits_both_retry_owners() -> None:
    dispatcher = WakeDispatcher(MagicMock(), MagicMock())
    started = asyncio.Event()
    finished: list[str] = []

    async def retry(name: str) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(name)

    composer = asyncio.create_task(retry("composer"))
    refresh = asyncio.create_task(retry("refresh"))
    dispatcher._composer_retries["proof"] = composer
    dispatcher._deferred_refreshes["proof"] = refresh
    await started.wait()
    try:
        await cancel_proof_retries(dispatcher, frozenset({"proof"}))
        assert composer.cancelled() and refresh.cancelled()
        assert sorted(finished) == ["composer", "refresh"]
    finally:
        composer.cancel()
        refresh.cancel()
        await asyncio.gather(composer, refresh, return_exceptions=True)


async def test_foreign_retry_prevents_any_cancellation() -> None:
    dispatcher = WakeDispatcher(MagicMock(), MagicMock())
    task = asyncio.create_task(pending_retry())
    dispatcher._composer_retries["foreign"] = task
    try:
        with pytest.raises(ProofRefused, match="unowned"):
            await cancel_proof_retries(dispatcher, frozenset({"proof"}))
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_private_cleanup_refuses_an_unowned_retry() -> None:
    dispatcher = WakeDispatcher(MagicMock(), MagicMock())
    retry = asyncio.create_task(pending_retry())
    dispatcher._composer_retries["foreign"] = retry
    with tempfile.TemporaryDirectory(prefix="p22915-unit-", dir="/tmp") as directory:
        control = CleanupControl(Path(directory) / "cleanup.sock")
        control.schedule(dispatcher)
        try:
            async with asyncio.timeout(2):
                await control.started.wait()
                with pytest.raises(ProofRefused, match="unconfirmed"):
                    await quiesce_proof(control.socket, [])
            assert not retry.done()
        finally:
            retry.cancel()
            if control.task is not None:
                control.task.cancel()
                await asyncio.gather(control.task, return_exceptions=True)
            await asyncio.gather(retry, return_exceptions=True)
