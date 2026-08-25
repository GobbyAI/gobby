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
import json
import logging
import threading
import time
from pathlib import Path

import pytest

from gobby.telemetry import loop_lag
from gobby.telemetry.loop_lag import (
    LoopLagReport,
    loop_lag_watchdog,
    measure_loop_lag,
)
from gobby.telemetry.loop_stack_sampler import LoopStackSampler

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
    payload_line = json.dumps(report.to_payload())
    assert "hog-the-loop" in payload_line
    assert "hog_the_loop" in payload_line


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
    payload = report.to_payload()
    assert "hog-the-loop" in json.dumps(payload["advanced"]), (
        "the payload must carry the suspect with its trace"
    )


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

    payload_line = json.dumps(reports[0].to_payload())
    assert "innermost_handler" in payload_line, (
        f"the report must reach the frame doing the work; got {payload_line!r}"
    )
    assert "outer_middleware" in payload_line, "and keep the chain that led there"


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


async def test_a_stall_report_carries_the_hot_python_stack_when_sampling() -> None:
    """A stall spread over ordinary Python needs frames, not just a task name.

    The daemon's worst stalls have no single blocking call to name -- their cost
    is spread across dict work, isinstance checks and GC. What identifies them
    is the Python stack the loop thread spent that time in, which only an
    off-loop sampler can see.
    """
    reports: list[LoopLagReport] = []
    stop = asyncio.Event()
    reported = asyncio.Event()
    released = asyncio.Event()
    start_burning = asyncio.Event()

    def record(report: LoopLagReport) -> None:
        reports.append(report)
        reported.set()

    def burn_in_a_named_frame() -> None:
        burn(0.5)

    async def hog_the_loop() -> None:
        await start_burning.wait()
        burn_in_a_named_frame()
        await released.wait()

    cycles = 0

    async def pace(_seconds: float) -> None:
        nonlocal cycles
        cycles += 1
        if cycles == 2:
            start_burning.set()
        await yield_to_loop()

    sampler = LoopStackSampler(threading.get_ident(), interval_seconds=0.002)
    sampler.start()
    watchdog = asyncio.create_task(
        loop_lag_watchdog(
            stop.is_set,
            threshold_seconds=0.2,
            poll_seconds=0.01,
            on_lag=record,
            sleep=pace,
            stack_sampler=sampler,
        )
    )
    hog = asyncio.create_task(hog_the_loop(), name="hog-the-loop")
    try:
        await asyncio.wait_for(reported.wait(), timeout=WATCHDOG_TIMEOUT_SECONDS)
    finally:
        stop.set()
        released.set()
        await asyncio.gather(watchdog, hog)
        sampler.stop()

    payload = reports[0].to_payload()
    payload_line = json.dumps(payload["hot_stacks"])
    assert "burn_in_a_named_frame" in payload_line, (
        f"the report must name the frame the loop thread burned in; got {payload_line!r}"
    )
    assert payload["sample_count"], "the report must carry the collected sample count"


def test_a_report_discloses_how_little_of_the_stall_it_actually_sampled() -> None:
    """A percentage over a handful of samples reads as authority it has not earned.

    Real reports carried 5 to 14 samples for stalls of 1.7 to 4 seconds, against
    a nominal 10ms interval that should have produced hundreds: the loop thread
    holds the GIL through long C calls, so the sampler is starved during exactly
    the events it exists to explain. Two chains that each held a large share of
    such a report measured 14 and 6 microseconds per call when timed directly --
    they were frequently executed, never expensive. The report has to show its
    own coverage so a share is read as a hypothesis (#20845).
    """
    report = LoopLagReport(
        lag_seconds=4.0,
        threshold_seconds=1.0,
        hot_stacks=[("a -> b", 9), ("c -> d", 5)],
        sample_interval_seconds=0.01,
    )

    payload = report.to_payload()
    assert payload["sample_count"] == 14
    assert payload["expected_sample_count"] == 400, (
        f"the report must say how many samples the interval should have produced; got {payload}"
    )


def test_the_summary_is_one_line_naming_the_gap_and_the_hottest_frame() -> None:
    """The main log gets the fact of the stall, never its contents (#20886)."""
    report = LoopLagReport(
        lag_seconds=1.5,
        threshold_seconds=0.2,
        advanced=[("worker", "step@mod.py:10 -> leaf@mod.py:20")],
        hot_stacks=[("outer@a.py -> middle@b.py -> inner@c.py", 9), ("other@d.py", 5)],
        sample_interval_seconds=0.01,
    )

    summary = report.summary()
    assert summary == ("Event loop stalled for 1.50s (threshold 0.20s); top hot frame: inner@c.py")

    unsampled = LoopLagReport(lag_seconds=1.5, threshold_seconds=0.2)
    assert unsampled.summary() == (
        "Event loop stalled for 1.50s (threshold 0.20s); no loop-thread samples"
    )


async def test_a_stall_lands_in_the_sidecar_with_a_one_line_pointer_in_the_main_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One stall: one JSON line in loop_lag.jsonl, one short WARNING in the main log.

    The full report -- stacks and task listings -- used to land in daemon.log as
    a multi-kilobyte WARNING. The fidelity moves to the JSONL sidecar; the main
    log keeps a single line naming the gap and pointing at it (#20886).
    """
    logs_dir = tmp_path / "logs"
    monkeypatch.setenv("GOBBY_LOGGING_DIR", str(logs_dir))
    module_logger = logging.getLogger("gobby.telemetry.loop_lag")
    caplog.set_level(logging.WARNING, logger=module_logger.name)

    stop = asyncio.Event()
    reported = asyncio.Event()
    released = asyncio.Event()
    warning_messages: list[str] = []

    class CaptureWarnings(logging.Handler):
        """The WARNING lands after the sidecar write, so it is the completion signal."""

        def emit(self, record: logging.LogRecord) -> None:
            warning_messages.append(record.getMessage())
            reported.set()

    capture = CaptureWarnings(level=logging.WARNING)
    module_logger.addHandler(capture)

    async def hog_the_loop() -> None:
        burn(0.5)
        await released.wait()

    sampler = LoopStackSampler(threading.get_ident(), interval_seconds=0.002)
    sampler.start()
    watchdog = asyncio.create_task(
        loop_lag_watchdog(
            stop.is_set,
            threshold_seconds=0.2,
            poll_seconds=0.01,
            stack_sampler=sampler,
        )
    )
    hog = asyncio.create_task(hog_the_loop(), name="hog-the-loop")
    try:
        await asyncio.wait_for(reported.wait(), timeout=WATCHDOG_TIMEOUT_SECONDS)
    finally:
        stop.set()
        released.set()
        await asyncio.gather(watchdog, hog)
        sampler.stop()
        module_logger.removeHandler(capture)

    sidecar = logs_dir / "loop_lag.jsonl"
    lines = sidecar.read_text().splitlines()
    assert len(lines) == 1, f"one stall writes exactly one sidecar line; got {len(lines)}"
    payload = json.loads(lines[0])
    assert payload["timestamp"]
    assert payload["lag_seconds"] >= 0.2
    assert payload["threshold_seconds"] == 0.2
    assert payload["hot_stacks"], "the sidecar keeps the sampled stacks"
    assert payload["sample_count"] == sum(entry["samples"] for entry in payload["hot_stacks"])
    activity = json.dumps(payload["advanced"] + payload["started"])
    assert "hog-the-loop" in activity, "the sidecar keeps the during-stall task activity"

    assert len(warning_messages) == 1, f"exactly one WARNING per stall; got {warning_messages!r}"
    message = warning_messages[0]
    assert "\n" not in message
    assert f"stalled for {payload['lag_seconds']:.2f}s" in message
    assert "(threshold 0.20s)" in message
    assert "top hot frame: " in message
    assert str(sidecar) in message, "the main log must point at the sidecar"
    assert " -> " not in message, "the main log must not carry a stack dump"
