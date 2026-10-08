"""Measure elapsed wall time without the spans the host spent asleep.

A wall-clock delta counts host sleep: after a laptop sleeps for an hour, every
idle check sees an hour of idleness its subjects never had (#23772).
``time.monotonic()`` stops while the host sleeps (``mach_absolute_time`` on macOS,
``CLOCK_MONOTONIC`` on Linux), so the wall-minus-monotonic offset grows by each
slept span. Sleep before this process started is not observed.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime

from gobby.utils.datetime import to_aware_utc

# Offset growth below this is clock slew or a small NTP step, not sleep.
MIN_SLEEP_SECONDS = 5.0


class AwakeClock:
    """Subtract observed host sleep from wall-clock deltas."""

    def __init__(
        self,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wall = wall
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._offset = wall() - monotonic()
        # (wall time the sleep was observed, seconds slept), oldest first.
        self._sleeps: deque[tuple[float, float]] = deque(maxlen=1024)

    def awake_seconds_since(self, start: datetime) -> float:
        """Return wall seconds since ``start`` minus host sleep inside that span."""
        since = to_aware_utc(start).timestamp()
        with self._lock:
            now = self._wall()
            offset = now - self._monotonic()
            if offset - self._offset >= MIN_SLEEP_SECONDS:
                self._sleeps.append((now, offset - self._offset))
            self._offset = offset
            slept = sum(min(span, max(0.0, woke - since)) for woke, span in self._sleeps)
        return now - since - slept


AWAKE_CLOCK = AwakeClock()
