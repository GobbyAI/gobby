"""Request-local timing for the hook execution path."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final

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
            return {"count": 0, "p50": 0.0, "p95": 0.0}
        return {
            "count": len(values),
            "p50": values[(len(values) - 1) // 2] * 1000,
            "p95": values[(95 * len(values) + 99) // 100 - 1] * 1000,
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
        with observe_queries(timings.add_query_latency):
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
