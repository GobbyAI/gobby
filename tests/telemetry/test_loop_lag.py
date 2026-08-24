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

from gobby.telemetry import loop_lag
from gobby.telemetry.loop_lag import (
    LoopLagReport,
    loop_lag_watchdog,
    measure_loop_lag,
)

pytestmark = pytest.mark.unit

# Comfortably longer than the stalls these tests create, so a hang fails loudly
# instead of hanging the suite.
WATCHDOG_TIMEOUT_SECONDS = 5.0


def burn(seconds: float) -> None:
    """Hold the loop synchronously for a span, the way a blocking call does."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass


async def yield_to_loop() -> None:
    """Give every other ready task one step, without waiting on the clock."""
    loop = asyncio.get_running_loop()
    resumed = loop.create_future()
    loop.call_soon(resumed.set_result, None)
    await resumed


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


async def test_the_task_that_ran_during_the_stall_is_singled_out() -> None:
    """Naming every live task names nothing; the report must isolate the suspect.

    While the loop is blocked nothing else gets a step, so the one task whose
    await position moved across the gap is the task that was executing. Tasks
    that merely sat suspended are bystanders and must be reported as such.

    The watchdog is paced by an injected yield so the baseline snapshot is taken
    on a quiet cycle, with both tasks already parked, before the burn starts.
    """
    reports: list[LoopLagReport] = []
    stop = asyncio.Event()
    reported = asyncio.Event()
    released = asyncio.Event()
    start_burning = asyncio.Event()
    bystander_may_finish = asyncio.Event()

    def record(report: LoopLagReport) -> None:
        reports.append(report)
        reported.set()

    async def hog_the_loop() -> None:
        await start_burning.wait()
        burn(0.5)
        await released.wait()

    async def merely_waiting() -> None:
        await bystander_may_finish.wait()

    cycles = 0

    async def pace(_seconds: float) -> None:
        """Yield to the loop, releasing the burn only after a quiet cycle."""
        nonlocal cycles
        cycles += 1
        if cycles == 2:
            start_burning.set()
        await yield_to_loop()

    watchdog = asyncio.create_task(
        loop_lag_watchdog(
            stop.is_set,
            threshold_seconds=0.2,
            poll_seconds=0.01,
            on_lag=record,
            sleep=pace,
        )
    )
    bystander = asyncio.create_task(merely_waiting(), name="merely-waiting")
    hog = asyncio.create_task(hog_the_loop(), name="hog-the-loop")
    try:
        await asyncio.wait_for(reported.wait(), timeout=WATCHDOG_TIMEOUT_SECONDS)
    finally:
        stop.set()
        released.set()
        bystander_may_finish.set()
        await asyncio.gather(watchdog, hog, bystander)

    report = reports[0]
    assert [name.split()[0] for name, _ in report.advanced] == ["hog-the-loop"], (
        f"only the burning task ran during the stall; got {report.advanced!r}"
    )
    assert report.started == [], "no task was created during this stall"
    bystander_names = [name.split()[0] for name in report.bystanders]
    assert "merely-waiting" in bystander_names, (
        f"a task that never moved is a bystander; got {report.bystanders!r}"
    )
    rendered = report.render()
    assert "ran during the stall" in rendered
    assert "hog-the-loop" in rendered


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


async def test_the_report_follows_the_await_chain_past_the_outer_task() -> None:
    """A task's own frame is not where the work is; the chain has to be walked.

    ``Task.get_stack`` returns only the task's own coroutine frame -- for a
    daemon request that is the outermost middleware, which names the framework
    instead of the code. The real caller chain lives behind ``cr_await``, and
    a report that stops at the task frame cannot locate a blocking call.
    """
    reports: list[LoopLagReport] = []
    stop = asyncio.Event()
    reported = asyncio.Event()
    released = asyncio.Event()
    start_burning = asyncio.Event()

    def record(report: LoopLagReport) -> None:
        reports.append(report)
        reported.set()

    async def innermost_handler() -> None:
        burn(0.5)
        await released.wait()

    async def middle_dispatch() -> None:
        await innermost_handler()

    async def outer_middleware() -> None:
        await start_burning.wait()
        await middle_dispatch()

    cycles = 0

    async def pace(_seconds: float) -> None:
        nonlocal cycles
        cycles += 1
        if cycles == 2:
            start_burning.set()
        await yield_to_loop()

    watchdog = asyncio.create_task(
        loop_lag_watchdog(
            stop.is_set,
            threshold_seconds=0.2,
            poll_seconds=0.01,
            on_lag=record,
            sleep=pace,
        )
    )
    request = asyncio.create_task(outer_middleware(), name="request")
    try:
        await asyncio.wait_for(reported.wait(), timeout=WATCHDOG_TIMEOUT_SECONDS)
    finally:
        stop.set()
        released.set()
        await asyncio.gather(watchdog, request)

    rendered = reports[0].render()
    assert "innermost_handler" in rendered, (
        f"the report must reach the frame doing the work; got {rendered!r}"
    )
    assert "outer_middleware" in rendered, "and keep the chain that led there"


async def test_quiet_polls_cost_nothing_beyond_the_position_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diagnostic that formats traces every cycle inflates the lag it measures.

    Building await-chain strings for every task that moved is affordable once
    per stall and not twenty times a second, so the per-poll path must stay out
    of it entirely.
    """
    calls = 0
    real_traces_for = loop_lag._traces_for

    def counting_traces_for(keys: set[str]) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return real_traces_for(keys)

    monkeypatch.setattr(loop_lag, "_traces_for", counting_traces_for)

    stop = asyncio.Event()
    reports: list[LoopLagReport] = []
    cycles = 0

    async def pace(_seconds: float) -> None:
        nonlocal cycles
        cycles += 1
        if cycles >= 10:
            stop.set()
        await yield_to_loop()

    # A task that steps every cycle, so tasks really are moving between polls.
    async def busy() -> None:
        while not stop.is_set():
            await yield_to_loop()

    worker = asyncio.create_task(busy(), name="busy")
    await asyncio.wait_for(
        loop_lag_watchdog(
            stop.is_set,
            threshold_seconds=5.0,
            poll_seconds=0.01,
            on_lag=reports.append,
            sleep=pace,
        ),
        timeout=WATCHDOG_TIMEOUT_SECONDS,
    )
    await worker

    assert cycles == 10
    assert reports == [], "no stall was created"
    assert calls == 0, f"quiet polls must not build traces; built them {calls} times"
