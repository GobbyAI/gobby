"""Deterministic stream-progress timing for chat backend tests.

The backends' progress deadline reads ``loop.time()`` and bounds each stdout read
with ``asyncio.wait_for``. These helpers replace the loop clock with one that
moves only when a scripted stdout line arrives, so a test asserts deadline
behaviour by event order rather than by wall-clock headroom.
"""

from __future__ import annotations

import asyncio

import pytest


class FakeLoopClock:
    """Loop time that advances only when a scripted line is released."""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def install_fake_loop_clock(monkeypatch: pytest.MonkeyPatch) -> FakeLoopClock:
    """Replace the running loop's clock for the rest of the test."""
    loop = asyncio.get_running_loop()
    clock = FakeLoopClock(loop.time())
    monkeypatch.setattr(loop, "time", clock)
    return clock


class ClockedStdout:
    """Each step is (gap_seconds, line), measured on the fake loop clock.

    A read advances the clock by the gap, then releases the line from a timer at
    the new time. The loop fires due timers in deadline order, so a progress
    deadline that expired within the gap cancels the read before the line lands.
    """

    def __init__(self, clock: FakeLoopClock, steps: list[tuple[float, str]]) -> None:
        self._clock = clock
        self._steps = list(steps)

    async def readline(self) -> bytes:
        if not self._steps:
            return b""
        gap, line = self._steps.pop(0)
        self._clock.now += gap
        loop = asyncio.get_running_loop()
        released: asyncio.Future[None] = loop.create_future()

        def release() -> None:
            # A deadline that fired first already cancelled this read.
            if not released.done():
                released.set_result(None)

        loop.call_at(self._clock.now, release)
        await released
        return (line + "\n").encode("utf-8")
