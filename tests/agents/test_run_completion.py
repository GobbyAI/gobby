"""Tests for thread-offloaded agent run completion helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gobby.agents.run_completion as run_completion

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_complete_and_notify_agent_run_offloads_complete_run() -> None:
    runner = MagicMock()
    runner.complete_run.return_value = True
    runner.get_run.return_value = SimpleNamespace(status="success")
    completion_registry = MagicMock()
    completion_registry.get_result.return_value = None
    completion_registry.notify = AsyncMock(return_value={})
    to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    async def fake_to_thread(
        func: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    run_completion.configure_terminal_delivery_offload(async_offload=fake_to_thread)
    try:
        completed = await run_completion.complete_and_notify_agent_run(
            runner,
            "run-123",
            completion_registry=completion_registry,
            notify_result={"status": "success"},
        )
    finally:
        run_completion.reset_terminal_delivery_offload()

    assert completed is True
    assert to_thread_calls[0] == (
        runner.complete_run,
        ("run-123",),
        {"result": None},
    )
    assert to_thread_calls[1][0].__name__ == "read_terminal_run"
    assert to_thread_calls[1][1:] == ((), {})
    runner.run_storage.db.bounded_transaction.assert_called_once_with()
    completion_registry.notify.assert_awaited_once_with(
        "run-123",
        result={"status": "success", "run_id": "run-123"},
        message="",
    )
    completion_registry.cleanup.assert_called_once_with("run-123")


@pytest.mark.asyncio
async def test_complete_and_notify_normalizes_a_copy_of_notify_result() -> None:
    runner = MagicMock()
    runner.complete_run.return_value = True
    runner.get_run.return_value = SimpleNamespace(status="success")
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock(return_value={})
    notify_result = {"status": "success", "run_id": "stale-run", "error": None}

    completed = await run_completion.complete_and_notify_agent_run(
        runner,
        "run-current",
        completion_registry=completion_registry,
        notify_result=notify_result,
    )

    assert completed is True
    assert notify_result == {"status": "success", "run_id": "stale-run", "error": None}
    completion_registry.notify.assert_awaited_once_with(
        "run-current",
        result={"status": "success", "run_id": "run-current"},
        message="",
    )


async def test_complete_and_notify_settles_delivery_before_cancellation() -> None:
    runner = MagicMock()
    runner.complete_run.return_value = True
    runner.get_run.return_value = SimpleNamespace(status="success")
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock(return_value={})
    started = asyncio.Event()
    release = asyncio.Event()

    async def offload(func, *args, **kwargs):
        if func is runner.complete_run:
            started.set()
            await release.wait()
        return func(*args, **kwargs)

    run_completion.configure_terminal_delivery_offload(async_offload=offload)
    try:
        owner = asyncio.create_task(
            run_completion.complete_and_notify_agent_run(
                runner,
                "run-cancelled",
                completion_registry=completion_registry,
                notify_result={"status": "success"},
            )
        )
        await started.wait()
        owner.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await owner
    finally:
        run_completion.reset_terminal_delivery_offload()

    completion_registry.notify.assert_awaited_once()
