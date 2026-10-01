from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from unittest.mock import call, patch

import pytest

from gobby.hooks import phase_timing
from gobby.hooks.phase_timing import (
    HOOK_PHASES,
    HookPhaseTimings,
    SlowHookLogSampler,
    SlowHookWindowSummary,
    hook_phase_timing_scope,
    measure_hook_phase,
    observe_hook_phase_timings,
    timed_await,
    timed_to_thread,
)
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def test_phase_scope_accumulates_nested_hook_work() -> None:
    timings = HookPhaseTimings()

    with hook_phase_timing_scope(timings):
        with measure_hook_phase("handler_body"):
            pass
        with measure_hook_phase("handler_body"):
            pass

    assert timings.snapshot()["handler_body"] > 0


@pytest.mark.asyncio
async def test_prelude_timing_separates_executor_queue_and_work() -> None:
    timings = HookPhaseTimings()

    async def immediate() -> int:
        return 7

    with hook_phase_timing_scope(timings):
        assert await timed_to_thread("prelude_probe", lambda: 42) == 42
        assert await timed_await("prelude_async", immediate()) == 7

    breakdown = timings.breakdown()
    assert breakdown["prelude_probe_queue"] >= 0
    assert breakdown["prelude_probe_work"] >= 0
    assert breakdown["prelude_probe"] >= breakdown["prelude_probe_queue"]
    assert breakdown["prelude_async"] >= 0


def test_hook_scope_reports_hub_query_percentiles(temp_db: HubDatabase) -> None:
    timings = HookPhaseTimings()

    with hook_phase_timing_scope(timings):
        assert temp_db.fetchone("SELECT 1 AS value") is not None
        assert temp_db.fetchone("SELECT 2 AS value") is not None

    summary = timings.query_latency_summary_ms()
    assert summary["count"] >= 2
    assert 0 < summary["p50"] <= summary["p95"]
    # Query timing excludes the wait for a pooled connection; it is its own key.
    assert timings.breakdown()["hub_pool_acquire"] > 0


def test_query_summary_exposes_blocked_query_hidden_past_p95() -> None:
    timings = HookPhaseTimings()
    for _ in range(33):
        timings.add_query_latency(0.002)
    timings.add_query_latency(4.4)

    summary = timings.query_latency_summary_ms()

    assert summary["count"] == 34
    assert summary["p95"] == pytest.approx(2.0)
    assert summary["max"] == pytest.approx(4400.0)


def test_observe_exports_all_phases_and_finds_dominant_phase() -> None:
    timings = HookPhaseTimings()
    timings.add("admission_wait", 6.0)
    timings.add("rule_evaluation", 1.0)

    with patch("gobby.hooks.phase_timing.observe_histogram") as observe:
        dominant, duration, phases = observe_hook_phase_timings(
            timings,
            total_seconds=8.0,
            hook_type="PreToolUse",
            source="codex",
        )

    assert (dominant, duration) == ("admission_wait", 6.0)
    assert phases["response"] == pytest.approx(1.0)
    assert observe.call_count == len(HOOK_PHASES)
    assert (
        call(
            "hook_phase_duration_seconds",
            6.0,
            attributes={
                "hook_type": "PreToolUse",
                "source": "codex",
                "phase": "admission_wait",
            },
        )
        in observe.call_args_list
    )


def test_slow_hook_sampler_logs_one_full_line_per_phase_per_window() -> None:
    sampler = SlowHookLogSampler(window_seconds=60.0)

    decisions = [
        sampler.observe(total_seconds=6.0, dominant_phase="admission_wait", now=0.0),
        sampler.observe(total_seconds=9.0, dominant_phase="admission_wait", now=10.0),
        sampler.observe(total_seconds=7.0, dominant_phase="rule_evaluation", now=20.0),
        sampler.observe(total_seconds=8.0, dominant_phase="admission_wait", now=30.0),
    ]

    assert [decision.log_full for decision in decisions] == [True, False, True, False]
    assert [decision.summary for decision in decisions] == [None] * 4


def test_slow_hook_sampler_summarizes_every_slow_hook_when_the_window_closes() -> None:
    sampler = SlowHookLogSampler(window_seconds=60.0)
    for now, seconds, phase in [
        (0.0, 6.0, "admission_wait"),
        (10.0, 9.5, "admission_wait"),
        (20.0, 7.0, "rule_evaluation"),
    ]:
        sampler.observe(total_seconds=seconds, dominant_phase=phase, now=now)

    # A fast hook after the window closes flushes the summary without counting.
    closing = sampler.observe(total_seconds=0.1, dominant_phase="response", now=61.0)
    after = sampler.observe(total_seconds=0.1, dominant_phase="response", now=200.0)

    assert closing.log_full is False
    assert closing.summary == SlowHookWindowSummary(
        count=3,
        suppressed=1,
        max_seconds=9.5,
        window_seconds=60.0,
        by_phase={"admission_wait": 2, "rule_evaluation": 1},
    )
    assert after.summary is None


def test_slow_hook_sampler_opens_a_fresh_window_on_the_closing_slow_hook() -> None:
    sampler = SlowHookLogSampler(window_seconds=60.0)
    sampler.observe(total_seconds=6.0, dominant_phase="admission_wait", now=0.0)

    rollover = sampler.observe(total_seconds=12.0, dominant_phase="admission_wait", now=75.0)
    closing = sampler.observe(total_seconds=0.1, dominant_phase="response", now=140.0)

    assert rollover.log_full is True
    assert rollover.summary is not None
    assert rollover.summary.count == 1
    assert closing.summary is not None
    assert (closing.summary.count, closing.summary.max_seconds) == (1, 12.0)


def test_slow_hook_sampler_ignores_fast_hooks() -> None:
    sampler = SlowHookLogSampler(window_seconds=60.0)

    decisions = [
        sampler.observe(total_seconds=4.9, dominant_phase="rule_evaluation", now=float(now))
        for now in (0, 30, 90, 200)
    ]

    assert all(not d.log_full and d.summary is None for d in decisions)


def test_slow_hook_sampler_close_window_reports_the_elapsed_partial_window() -> None:
    sampler = SlowHookLogSampler(window_seconds=60.0)
    assert sampler.close_window(now=5.0) is None

    sampler.observe(total_seconds=6.0, dominant_phase="admission_wait", now=10.0)
    sampler.observe(total_seconds=8.0, dominant_phase="admission_wait", now=20.0)

    assert sampler.close_window(now=35.0) == SlowHookWindowSummary(
        count=2,
        suppressed=1,
        max_seconds=8.0,
        window_seconds=25.0,
        by_phase={"admission_wait": 2},
    )
    assert sampler.close_window(now=36.0) is None


@dataclass
class _ManualTimer:
    when: float
    callback: Callable[[], object]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class _ManualLoop:
    """A loop clock the test advances by hand; timers fire only when told to."""

    now: float = 0.0
    timers: list[_ManualTimer] = field(default_factory=list)

    def time(self) -> float:
        return self.now

    def call_later(self, delay: float, callback: Callable[[], object]) -> _ManualTimer:
        timer = _ManualTimer(when=self.now + delay, callback=callback)
        self.timers.append(timer)
        return timer

    def pending(self) -> list[_ManualTimer]:
        return [timer for timer in self.timers if not timer.cancelled]

    def fire_at(self, now: float) -> None:
        self.now = now
        (timer,) = self.pending()
        self.timers.remove(timer)
        timer.callback()


def _reporter(
    loop: _ManualLoop,
) -> tuple[phase_timing.SlowHookSummaryReporter, list[SlowHookWindowSummary]]:
    emitted: list[SlowHookWindowSummary] = []
    reporter = phase_timing.SlowHookSummaryReporter(
        emit=emitted.append,
        sampler=SlowHookLogSampler(window_seconds=60.0),
        loop_factory=lambda: loop,
    )
    return reporter, emitted


def test_slow_hook_reporter_delivers_an_idle_window_at_its_deadline() -> None:
    loop = _ManualLoop(now=100.0)
    reporter, emitted = _reporter(loop)

    assert reporter.observe(total_seconds=6.0, dominant_phase="admission_wait") is True
    loop.now = 110.0
    assert reporter.observe(total_seconds=7.0, dominant_phase="admission_wait") is False

    # One timer, bounded by the window that opened at 100.
    assert [timer.when for timer in loop.pending()] == [160.0]
    loop.fire_at(160.0)

    assert emitted == [
        SlowHookWindowSummary(
            count=2,
            suppressed=1,
            max_seconds=7.0,
            window_seconds=60.0,
            by_phase={"admission_wait": 2},
        )
    ]
    assert loop.pending() == []


def test_slow_hook_reporter_rearms_when_its_timer_fires_before_the_deadline() -> None:
    loop = _ManualLoop()
    reporter, emitted = _reporter(loop)
    reporter.observe(total_seconds=6.0, dominant_phase="rule_evaluation")

    # asyncio may run a timer up to one clock resolution early.
    loop.fire_at(59.999)
    assert emitted == []
    assert [timer.when for timer in loop.pending()] == [60.0]

    loop.fire_at(60.0)
    assert [summary.count for summary in emitted] == [1]


def test_slow_hook_reporter_does_not_arm_for_fast_hooks() -> None:
    loop = _ManualLoop()
    reporter, emitted = _reporter(loop)

    assert reporter.observe(total_seconds=0.2, dominant_phase="response") is False

    assert loop.pending() == []
    reporter.close()
    assert emitted == []


def test_slow_hook_reporter_close_drains_the_open_window_and_cancels_its_timer() -> None:
    loop = _ManualLoop()
    reporter, emitted = _reporter(loop)
    reporter.observe(total_seconds=6.0, dominant_phase="admission_wait")
    loop.now = 12.0
    reporter.observe(total_seconds=9.0, dominant_phase="rule_evaluation")

    reporter.close()
    reporter.close()

    assert emitted == [
        SlowHookWindowSummary(
            count=2,
            suppressed=0,
            max_seconds=9.0,
            window_seconds=12.0,
            by_phase={"admission_wait": 1, "rule_evaluation": 1},
        )
    ]
    assert loop.pending() == []
