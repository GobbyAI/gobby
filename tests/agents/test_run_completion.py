"""Tests for thread-offloaded agent run completion helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import gobby.agents.run_completion as run_completion

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_complete_and_notify_agent_run_offloads_complete_run() -> None:
    runner = MagicMock()
    runner.complete_run.return_value = True
    completion_registry = MagicMock()
    completion_registry.get_result.return_value = None
    completion_registry.notify = AsyncMock()
    to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    async def fake_to_thread(func: object, *args: object, **kwargs: object) -> object:
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    original_to_thread = run_completion.asyncio.to_thread
    run_completion.asyncio.to_thread = fake_to_thread
    try:
        completed = await run_completion.complete_and_notify_agent_run(
            runner,
            "run-123",
            completion_registry=completion_registry,
            notify_result={"status": "success"},
        )
    finally:
        run_completion.asyncio.to_thread = original_to_thread

    assert completed is True
    assert to_thread_calls == [
        (
            runner.complete_run,
            ("run-123",),
            {"result": None},
        )
    ]
    completion_registry.notify.assert_awaited_once_with(
        "run-123",
        {"status": "success"},
        message="",
    )
