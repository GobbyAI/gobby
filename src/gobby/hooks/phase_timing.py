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
    _lock: threading.Lock = field(default_factory=threading.Lock)

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


_current_timings: ContextVar[HookPhaseTimings | None] = ContextVar(
    "hook_phase_timings",
    default=None,
)


@contextmanager
def hook_phase_timing_scope(timings: HookPhaseTimings) -> Iterator[None]:
    """Expose one delivery's collector across hook and workflow calls."""
    token = _current_timings.set(timings)
    try:
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
