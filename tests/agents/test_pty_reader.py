"""Focused lifecycle tests for daemon-owned PTY output readers."""

from __future__ import annotations

import asyncio

import pytest

from gobby.agents.pty_reader import PTYReaderManager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_stop_reader_drains_in_flight_callback() -> None:
    reader = PTYReaderManager()
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    callback_finished = asyncio.Event()

    async def in_flight_callback() -> None:
        callback_started.set()
        await release_callback.wait()
        callback_finished.set()

    task = asyncio.create_task(in_flight_callback())
    reader._reader_tasks["run-123"] = task
    stop_event = asyncio.Event()
    reader._stop_events["run-123"] = stop_event
    await callback_started.wait()

    stop_task = asyncio.create_task(reader.stop_reader("run-123"))
    await asyncio.wait_for(stop_event.wait(), timeout=1.0)

    assert stop_task.done() is False
    assert callback_finished.is_set() is False

    release_callback.set()
    assert await asyncio.wait_for(stop_task, timeout=1.0) is True
    assert callback_finished.is_set() is True
