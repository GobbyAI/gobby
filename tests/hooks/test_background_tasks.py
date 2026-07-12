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
