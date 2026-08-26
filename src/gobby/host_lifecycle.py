"""Host sleep and wake lifecycle helpers."""

from __future__ import annotations

import time
from collections.abc import Callable


class HostSleepTracker:
    """Track a short recovery window after the host resumes from sleep.

    ``time.monotonic()`` pauses during system suspend on supported platforms while
    wall time continues. Comparing their elapsed values detects that discontinuity
    without platform-specific power-management APIs.
    """

    def __init__(
        self,
        *,
        suspend_threshold_seconds: float = 5.0,
        resume_grace_seconds: float = 60.0,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._suspend_threshold_seconds = suspend_threshold_seconds
        self._resume_grace_seconds = resume_grace_seconds
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._last_wall = wall_clock()
        self._last_monotonic = monotonic_clock()
        self._resume_grace_until = self._last_monotonic

    def observe_resume(self) -> bool:
        """Observe both clocks and report whether resume grace is active."""
        wall_now = self._wall_clock()
        monotonic_now = self._monotonic_clock()
        wall_elapsed = max(0.0, wall_now - self._last_wall)
        monotonic_elapsed = max(0.0, monotonic_now - self._last_monotonic)
        suspended_seconds = wall_elapsed - monotonic_elapsed

        self._last_wall = wall_now
        self._last_monotonic = monotonic_now
        if suspended_seconds >= self._suspend_threshold_seconds:
            self._resume_grace_until = monotonic_now + self._resume_grace_seconds

        return monotonic_now < self._resume_grace_until
