"""Awake-time measurement that excludes host sleep (#23772)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.utils.host_sleep import MIN_SLEEP_SECONDS, AwakeClock

pytestmark = pytest.mark.unit


class _Host:
    """Wall and monotonic clocks of a host that runs and sleeps."""

    def __init__(self) -> None:
        self.wall = 1_000.0
        self.monotonic = 0.0

    def run(self, seconds: float) -> None:
        self.wall += seconds
        self.monotonic += seconds

    def sleep(self, seconds: float) -> None:
        self.wall += seconds

    def clock(self) -> AwakeClock:
        return AwakeClock(wall=lambda: self.wall, monotonic=lambda: self.monotonic)


def _at(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


def test_awake_seconds_exclude_observed_sleep() -> None:
    host = _Host()
    clock = host.clock()
    host.run(100)
    host.sleep(3_600)
    assert clock.awake_seconds_since(_at(1_000)) == 100

    host.run(50)
    assert clock.awake_seconds_since(_at(1_000)) == 150
    assert clock.awake_seconds_since(_at(2_900)) == 50  # started mid-sleep
    assert clock.awake_seconds_since(_at(4_720)) == 30  # started after the wake


def test_offset_drift_below_threshold_is_not_sleep() -> None:
    host = _Host()
    clock = host.clock()
    host.run(100)
    host.sleep(MIN_SLEEP_SECONDS - 1)
    assert clock.awake_seconds_since(_at(1_000)) == 100 + MIN_SLEEP_SECONDS - 1
