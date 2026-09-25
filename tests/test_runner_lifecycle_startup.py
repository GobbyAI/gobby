"""Runner startup leaves the event loop free of a lag watcher."""

import asyncio
import threading
from typing import cast

import pytest

import gobby.runner_lifecycle as lifecycle
from gobby.runner import GobbyRunner

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_subsystem_startup_does_not_schedule_lag_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = cast(GobbyRunner, object())
    rebuild_vector_store = object()
    loop = asyncio.get_running_loop()
    scheduled_before = tuple(vars(loop)["_scheduled"])
    threads_before = {thread.ident for thread in threading.enumerate()}
    initialized = False

    async def initialize(*args: object, **kwargs: object) -> None:
        nonlocal initialized
        initialized = True

    monkeypatch.setattr(lifecycle, "init_subsystems", initialize)
    await lifecycle._init_subsystems(runner, rebuild_vector_store)

    assert initialized
    assert tuple(vars(loop)["_scheduled"]) == scheduled_before
    assert {thread.ident for thread in threading.enumerate()} == threads_before
