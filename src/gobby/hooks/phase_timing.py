"""Request-local timing for the hook execution path."""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from gobby.telemetry.instruments import observe_histogram
from gobby.telemetry.query_timing import observe_queries

HOOK_PHASES: Final = (
    "admission_wait",
    "executor_queue",
    "session_resolution",
    "rule_evaluation",
    "handler_body",
    "persistence_broadcast",
    "response",
)
SLOW_HOOK_THRESHOLD_SECONDS: Final = 5.0
SLOW_HOOK_LOG_WINDOW_SECONDS: Final = 60.0


@dataclass
class HookPhaseTimings:
    """Thread-safe phase durations shared by one hook delivery."""

    _durations: dict[str, float] = field(default_factory=dict)
    _query_latencies: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # The Gobby session the rules evaluated for; rule-allow-audit rows carry the same id.
    session_id: str | None = None

    def add(self, phase: str, duration_seconds: float) -> None:
        with self._lock:
            self._durations[phase] = self._durations.get(phase, 0.0) + max(0.0, duration_seconds)

    @contextmanager
    def measure(self, phase: str) -> Iterator[None]:
        started_at = time.perf_counter()
        try:
            yield
        finally:
            self.add(phase, time.perf_counter() - started_at)

    def snapshot(self, *, total_seconds: float | None = None) -> dict[str, float]:
        with self._lock:
            durations = {phase: self._durations.get(phase, 0.0) for phase in HOOK_PHASES}
        if total_seconds is not None:
            measured = sum(value for phase, value in durations.items() if phase != "response")
            durations["response"] = max(durations["response"], total_seconds - measured)
        return durations

    def breakdown(self) -> dict[str, float]:
        """Sub-phases recorded inside a phase; they stay out of snapshot and dominance."""
        with self._lock:
            return {
                phase: value for phase, value in self._durations.items() if phase not in HOOK_PHASES
            }

    def add_query_latency(self, duration_seconds: float) -> None:
        with self._lock:
            self._query_latencies.append(max(0.0, duration_seconds))

    def query_latency_summary_ms(self) -> dict[str, float | int]:
        with self._lock:
            values = sorted(self._query_latencies)
        if not values:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        # Below 20 queries p95 is the max; above it, one lock-blocked query hides past p95.
        return {
            "count": len(values),
            "p50": values[(len(values) - 1) // 2] * 1000,
            "p95": values[(95 * len(values) + 99) // 100 - 1] * 1000,
            "max": values[-1] * 1000,
        }


_current_timings: ContextVar[HookPhaseTimings | None] = ContextVar(
    "hook_phase_timings",
    default=None,
)


@contextmanager
def hook_phase_timing_scope(timings: HookPhaseTimings) -> Iterator[None]:
    """Expose one delivery's collector across hook and workflow calls."""
    token = _current_timings.set(timings)
    try:
        with observe_queries(
            timings.add_query_latency,
            pool_acquire_observer=functools.partial(timings.add, "hub_pool_acquire"),
        ):
            yield
    finally:
        _current_timings.reset(token)


@contextmanager
def measure_hook_phase(phase: str) -> Iterator[None]:
    """Measure a phase when called inside an active hook delivery."""
    timings = _current_timings.get()
    if timings is None:
        yield
        return
    with timings.measure(phase):
        yield


def add_hook_phase(phase: str, duration_seconds: float) -> None:
    """Record a duration measured across awaits inside an active hook delivery."""
    timings = _current_timings.get()
    if timings is not None:
        timings.add(phase, duration_seconds)


async def timed_to_thread[T](
    phase: str, function: Callable[..., T], /, *args: Any, **kwargs: Any
) -> T:
    """Separate default-executor queue delay from the blocking call's work."""
    queued_at = time.perf_counter()

    def invoke() -> T:
        started_at = time.perf_counter()
        add_hook_phase(f"{phase}_queue", started_at - queued_at)
        try:
            return function(*args, **kwargs)
        finally:
            add_hook_phase(f"{phase}_work", time.perf_counter() - started_at)

    with measure_hook_phase(phase):
        return await asyncio.to_thread(invoke)


async def timed_await[T](phase: str, awaitable: Awaitable[T]) -> T:
    with measure_hook_phase(phase):
        return await awaitable


def note_hook_session(session_id: str) -> None:
    """Name the session whose rules this hook delivery evaluates."""
    timings = _current_timings.get()
    if timings is not None:
        timings.session_id = session_id


def observe_hook_phase_timings(
    timings: HookPhaseTimings,
    *,
    total_seconds: float,
    hook_type: str | None,
    source: str | None,
) -> tuple[str, float, dict[str, float]]:
    """Export every phase and return the dominant one for slow-hook logging."""
    durations = timings.snapshot(total_seconds=total_seconds)
    attributes = {"hook_type": hook_type or "unknown", "source": source or "unknown"}
    for phase, duration in durations.items():
        observe_histogram(
            "hook_phase_duration_seconds",
            duration,
            attributes={**attributes, "phase": phase},
        )
    dominant_phase, dominant_seconds = max(durations.items(), key=lambda item: item[1])
    return dominant_phase, dominant_seconds, durations


@dataclass(frozen=True)
class SlowHookWindowSummary:
    """Every slow hook in one sampling window, including suppressed WARNING lines."""

    count: int
    suppressed: int
    max_seconds: float
    window_seconds: float
    by_phase: dict[str, int]


@dataclass(frozen=True)
class SlowHookLogDecision:
    log_full: bool
    summary: SlowHookWindowSummary | None


@dataclass
class SlowHookLogSampler:
    """Bound slow-hook WARNING volume without losing the count (#22866).

    Observe every hook. Within a window, only the first slow hook per dominant
    phase gets its full WARNING; the window's summary, returned by the first
    observation after it closes, still counts every slow hook.
    """

    window_seconds: float = SLOW_HOOK_LOG_WINDOW_SECONDS
    _window_start: float | None = None
    _by_phase: dict[str, int] = field(default_factory=dict)
    _suppressed: int = 0
    _max_seconds: float = 0.0

    def window_deadline(self) -> float | None:
        if self._window_start is None:
            return None
        return self._window_start + self.window_seconds

    def close_due_window(self, now: float) -> SlowHookWindowSummary | None:
        deadline = self.window_deadline()
        if deadline is None or now < deadline:
            return None
        return self.close_window(now)

    def close_window(self, now: float) -> SlowHookWindowSummary | None:
        """Summarize the open window, reporting how long it actually covered."""
        if self._window_start is None:
            return None
        summary = SlowHookWindowSummary(
            count=sum(self._by_phase.values()),
            suppressed=self._suppressed,
            max_seconds=self._max_seconds,
            window_seconds=min(now - self._window_start, self.window_seconds),
            by_phase=dict(self._by_phase),
        )
        self._window_start = None
        self._by_phase = {}
        self._suppressed = 0
        self._max_seconds = 0.0
        return summary

    def observe(
        self, *, total_seconds: float, dominant_phase: str, now: float
    ) -> SlowHookLogDecision:
        summary = self.close_due_window(now)
        if total_seconds < SLOW_HOOK_THRESHOLD_SECONDS:
            return SlowHookLogDecision(log_full=False, summary=summary)
        if self._window_start is None:
            self._window_start = now
        log_full = dominant_phase not in self._by_phase
        self._by_phase[dominant_phase] = self._by_phase.get(dominant_phase, 0) + 1
        self._max_seconds = max(self._max_seconds, total_seconds)
        if not log_full:
            self._suppressed += 1
        return SlowHookLogDecision(log_full=log_full, summary=summary)


class _SummaryTimer(Protocol):
    def cancel(self) -> None: ...


class _SummaryLoop(Protocol):
    def time(self) -> float: ...

    def call_later(self, delay: float, callback: Callable[[], object], /) -> _SummaryTimer: ...


@dataclass
class SlowHookSummaryReporter:
    """Deliver each window summary by its deadline, whether or not another hook arrives.

    The sampler only produces a summary inside a later ``observe``, so an idle
    daemon would hold the last window indefinitely and a shutdown would drop it.
    A loop timer armed at the window deadline bounds the delay; ``close`` drains
    the open window when the app stops.
    """

    emit: Callable[[SlowHookWindowSummary], None]
    sampler: SlowHookLogSampler = field(default_factory=SlowHookLogSampler)
    loop_factory: Callable[[], _SummaryLoop] = asyncio.get_running_loop
    _loop: _SummaryLoop | None = None
    _timer: _SummaryTimer | None = None

    def observe(self, *, total_seconds: float, dominant_phase: str) -> bool:
        """Count one hook; return whether it gets the full slow-hook WARNING."""
        self._loop = self.loop_factory()
        decision = self.sampler.observe(
            total_seconds=total_seconds, dominant_phase=dominant_phase, now=self._loop.time()
        )
        self._deliver(decision.summary)
        self._arm()
        return decision.log_full

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._loop is not None:
            self._deliver(self.sampler.close_window(self._loop.time()))

    def _arm(self) -> None:
        deadline = self.sampler.window_deadline()
        if deadline is None or self._timer is not None or self._loop is None:
            return
        self._timer = self._loop.call_later(max(0.0, deadline - self._loop.time()), self._flush_due)

    def _flush_due(self) -> None:
        # asyncio may run a timer one clock resolution early, and a hook may have
        # rolled the window over since arming, so re-arm for any open window.
        self._timer = None
        if self._loop is not None:
            self._deliver(self.sampler.close_due_window(self._loop.time()))
        self._arm()

    def _deliver(self, summary: SlowHookWindowSummary | None) -> None:
        if summary is not None:
            self.emit(summary)
