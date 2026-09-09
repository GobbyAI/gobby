"""Async worktree handlers stay on the request loop after hook preparation."""

import asyncio
import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager


async def test_async_handler_runs_on_request_loop_after_threaded_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    request_thread = threading.get_ident()
    preparation_threads: list[int] = []
    started = asyncio.Event()
    release = asyncio.Event()
    response = HookResponse(decision="allow", worktree_path="/tmp/created")

    async def handle() -> HookResponse:
        assert asyncio.get_running_loop() is loop
        assert threading.get_ident() == request_thread
        started.set()
        await release.wait()
        return response

    def prepare(_event: HookEvent) -> object:
        preparation_threads.append(threading.get_ident())
        return handle()

    manager = cast(
        HookManager,
        SimpleNamespace(
            _health_monitor=None,
            logger=logging.getLogger(__name__),
            _handle_after_daemon_ready=prepare,
        ),
    )
    monkeypatch.setattr(
        "gobby.hooks.hook_manager.ensure_daemon_ready_async", AsyncMock(return_value=None)
    )
    event = HookEvent(
        event_type=HookEventType.WORKTREE_CREATE,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        machine_id="21000000-0000-4000-8000-000000000002",
    )
    request = asyncio.create_task(HookManager._handle_internal_async(manager, event))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert not request.done()
        assert preparation_threads and request_thread not in preparation_threads
    finally:
        release.set()
    assert await request is response
