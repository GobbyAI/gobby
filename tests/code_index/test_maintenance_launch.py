"""Cancellation-safe cleanup for maintenance launch contexts."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from gobby.code_index.maintenance_launch import open_launch_async
from gobby.runtime_grants.launch import ManagedLaunch

pytestmark = pytest.mark.unit


class _RecordingExit:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.exit_started = threading.Event()
        self.exited = threading.Event()
        self.release = threading.Event()

    def __enter__(self) -> ManagedLaunch:
        self.entered.set()
        return MagicMock(spec=ManagedLaunch)

    def __exit__(self, *_args: object) -> None:
        self.exit_started.set()
        self.release.wait(timeout=1)
        self.exited.set()


@pytest.mark.asyncio
async def test_open_launch_async_exit_survives_cancellation() -> None:
    recording = _RecordingExit()

    class _Factory:
        def open(self, project_id: str, *, timeout_seconds: float) -> _RecordingExit:
            del project_id, timeout_seconds
            return recording

    async def _run() -> None:
        async with open_launch_async(_Factory(), "project", timeout_seconds=1.0):
            await asyncio.Event().wait()

    task = asyncio.create_task(_run())
    await asyncio.to_thread(recording.entered.wait, 1)
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.to_thread(recording.exit_started.wait, 1)
    recording.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recording.exited.is_set()
