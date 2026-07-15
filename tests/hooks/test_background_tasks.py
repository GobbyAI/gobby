"""Tests for hook background task retention."""

import asyncio
import gc
import weakref

import pytest

from gobby.hooks import background_tasks

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_task_is_retained_until_done_then_discarded() -> None:
    release = asyncio.Event()
    callback_complete = asyncio.Event()

    task = background_tasks.create_background_task(release.wait())
    task_ref = weakref.ref(task)
    task.add_done_callback(lambda _task: callback_complete.set())
    del task
    gc.collect()

    assert task_ref() is not None
    assert len(background_tasks._background_tasks) == 1

    release.set()
    await callback_complete.wait()

    assert not background_tasks._background_tasks


@pytest.mark.asyncio
async def test_task_exception_is_observed_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def fail() -> None:
        raise RuntimeError("background failure")

    task = background_tasks.create_background_task(fail())
    callback_complete = asyncio.Event()
    task.add_done_callback(lambda _task: callback_complete.set())

    with pytest.raises(RuntimeError, match="background failure"):
        await task
    await callback_complete.wait()

    assert not background_tasks._background_tasks
    assert "Background hook task failed" in caplog.text
    assert "background failure" in caplog.text
