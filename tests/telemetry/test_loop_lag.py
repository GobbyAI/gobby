"""The loop-lag watchdog must name what stalled the loop, not just that it did.

sample(1) proves the daemon's loop thread runs CPU-bound Python for seconds at a
time but collapses every Python frame into ``_PyEval_EvalFrameDefault``, and
py-spy needs root on macOS. The daemon has to be able to answer this itself
(#20841), which means reporting the live tasks alongside the gap.

Every case below drives the watchdog by event rather than by waiting a guessed
interval: a stall is produced by burning the loop for a known span, and the
report itself is the signal the test waits on.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from gobby.telemetry.loop_lag import LoopLagReport, loop_lag_watchdog, measure_loop_lag

pytestmark = pytest.mark.unit

# Comfortably longer than the stalls these tests create, so a hang fails loudly
# instead of hanging the suite.
WATCHDOG_TIMEOUT_SECONDS = 5.0


def burn(seconds: float) -> None:
    """Hold the loop synchronously for a span, the way a blocking call does."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass


async def test_a_loop_that_comes_back_late_but_under_threshold_is_not_reported() -> None:
    """Ordinary scheduling delay is not a stall; only the threshold makes it one."""
    reports: list[LoopLagReport] = []
    stop = asyncio.Event()
    cycles = 0

    async def late_by_less_than_the_threshold(_seconds: float) -> None:
        nonlocal cycles
        cycles += 1
        burn(0.05)
        if cycles >= 5:
            stop.set()

    await asyncio.wait_for(
        loop_lag_watchdog(
            stop.is_set,
            threshold_seconds=0.5,
            poll_seconds=0.01,
            on_lag=reports.append,
            sleep=late_by_less_than_the_threshold,
        ),
        timeout=WATCHDOG_TIMEOUT_SECONDS,
    )

    assert cycles == 5, "the watchdog must keep polling until shutdown is requested"
    assert reports == []


async def test_a_blocked_loop_is_reported_with_the_task_that_blocked_it() -> None:
    """A synchronous burn is caught, measured, and named while its task lives on.

    A loop-based watchdog cannot run *during* the block, so it can only name
    tasks that are still alive once the loop comes back. That covers the case
    this exists for -- a long-running request or spawn that burns partway
    through and then carries on.
    """
    reports: list[LoopLagReport] = []
    stop = asyncio.Event()
    reported = asyncio.Event()
    released = asyncio.Event()

    def record(report: LoopLagReport) -> None:
        reports.append(report)
        reported.set()

    async def hog_the_loop() -> None:
        burn(0.5)
        await released.wait()

    watchdog = asyncio.create_task(
        loop_lag_watchdog(
            stop.is_set,
            threshold_seconds=0.2,
            poll_seconds=0.01,
            on_lag=record,
        )
    )
    hog = asyncio.create_task(hog_the_loop(), name="hog-the-loop")
    try:
        await asyncio.wait_for(reported.wait(), timeout=WATCHDOG_TIMEOUT_SECONDS)
    finally:
        stop.set()
        released.set()
        await asyncio.gather(watchdog, hog)

    report = reports[0]
    assert report.lag_seconds >= 0.2
    rendered = report.render()
    assert "hog-the-loop" in rendered
    assert "hog_the_loop" in rendered


async def test_a_stall_is_reported_once_rather_than_every_poll() -> None:
    """One report per stall, so a pathological loop cannot flood the log."""
    reports: list[LoopLagReport] = []
    stop = asyncio.Event()
    cycles = 0

    async def stall_every_cycle(_seconds: float) -> None:
        nonlocal cycles
        cycles += 1
        burn(0.3)
        if cycles >= 4:
            stop.set()

    await asyncio.wait_for(
        loop_lag_watchdog(
            stop.is_set,
            threshold_seconds=0.2,
            poll_seconds=0.01,
            rearm_seconds=30.0,
            on_lag=reports.append,
            sleep=stall_every_cycle,
        ),
        timeout=WATCHDOG_TIMEOUT_SECONDS,
    )

    assert cycles == 4, "four consecutive stalls must each be observed"
    assert len(reports) == 1, "but the re-arm window must collapse them into one report"


async def test_measure_loop_lag_excludes_the_sleep_it_asked_for() -> None:
    """Lag is scheduling delay beyond the requested sleep, not the sleep itself."""
    lag = await measure_loop_lag(0.05)
    assert lag >= 0.0
    assert lag < 0.5
