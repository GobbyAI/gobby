"""Ownership of compact continuations queued on an event loop."""

import asyncio
import inspect

import pytest

from gobby.sessions.compact_continuation import _schedule_coroutine

pytestmark = pytest.mark.unit


def test_closing_loop_closes_unstarted_continuation() -> None:
    async def continuation() -> None:
        raise AssertionError("a discarded continuation must never execute")

    loop = asyncio.new_event_loop()
    coro = continuation()
    try:
        assert _schedule_coroutine(coro, loop=loop) is True
        loop.close()
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    finally:
        coro.close()
        loop.close()


def test_queued_continuation_executes_once_when_loop_runs() -> None:
    completed: list[str] = []

    async def continuation() -> None:
        completed.append("completed")

    loop = asyncio.new_event_loop()
    coro = continuation()
    try:
        assert _schedule_coroutine(coro, loop=loop) is True
        loop.run_until_complete(asyncio.sleep(0))
        assert completed == ["completed"]
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    finally:
        coro.close()
        loop.close()


def test_rejected_queue_closes_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def continuation() -> None:
        raise AssertionError("a rejected continuation must never execute")

    def reject(*args: object, **kwargs: object) -> None:
        raise RuntimeError("event loop stopped accepting work")

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(loop, "call_soon_threadsafe", reject)
    coro = continuation()
    try:
        assert _schedule_coroutine(coro, loop=loop) is False
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    finally:
        coro.close()
        loop.close()
