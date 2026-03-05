"""Tests for pipeline background task cleanup."""

from __future__ import annotations

import asyncio

import pytest

from gobby.mcp_proxy.tools.workflows._pipeline_execution import (
    _background_tasks,
    cleanup_background_tasks,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_background_tasks() -> None:
    """Ensure _background_tasks is empty before and after each test."""
    _background_tasks.clear()
    yield  # type: ignore[misc]
    _background_tasks.clear()


@pytest.mark.asyncio
async def test_cleanup_cancels_pending_tasks() -> None:
    """cleanup_background_tasks cancels all tracked tasks."""
    ev = asyncio.Event()

    async def _block() -> None:
        await ev.wait()

    task = asyncio.create_task(_block())
    _background_tasks.add(task)

    await cleanup_background_tasks()

    assert task.cancelled()
    assert len(_background_tasks) == 0


@pytest.mark.asyncio
async def test_cleanup_noop_when_empty() -> None:
    """cleanup_background_tasks is a no-op when set is empty."""
    await cleanup_background_tasks()
    assert len(_background_tasks) == 0


@pytest.mark.asyncio
async def test_cleanup_handles_already_finished_tasks() -> None:
    """cleanup_background_tasks handles tasks that already completed."""

    async def _instant() -> None:
        pass

    task = asyncio.create_task(_instant())
    _background_tasks.add(task)
    await task  # let it finish

    await cleanup_background_tasks()
    assert len(_background_tasks) == 0


@pytest.mark.asyncio
async def test_cleanup_handles_errored_tasks() -> None:
    """cleanup_background_tasks logs but doesn't raise on task errors."""

    async def _fail() -> None:
        raise RuntimeError("boom")

    task = asyncio.create_task(_fail())
    _background_tasks.add(task)

    # Wait for the task to complete (will raise internally)
    await asyncio.gather(task, return_exceptions=True)

    # Should not raise
    await cleanup_background_tasks()
    assert len(_background_tasks) == 0


@pytest.mark.asyncio
async def test_cleanup_clears_set() -> None:
    """cleanup_background_tasks clears the _background_tasks set."""
    ev = asyncio.Event()

    async def _block() -> None:
        await ev.wait()

    for _ in range(3):
        task = asyncio.create_task(_block())
        _background_tasks.add(task)

    assert len(_background_tasks) == 3

    await cleanup_background_tasks()
    assert len(_background_tasks) == 0
