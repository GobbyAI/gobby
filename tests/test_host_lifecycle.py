from __future__ import annotations

import pytest

from gobby.host_lifecycle import HostSleepTracker

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self) -> None:
        self.wall = 100.0
        self.monotonic = 10.0

    def advance(self, *, wall: float, monotonic: float) -> None:
        self.wall += wall
        self.monotonic += monotonic


def test_host_sleep_tracker_detects_suspend_and_expires_resume_grace() -> None:
    clock = _Clock()
    tracker = HostSleepTracker(
        suspend_threshold_seconds=5.0,
        resume_grace_seconds=60.0,
        wall_clock=lambda: clock.wall,
        monotonic_clock=lambda: clock.monotonic,
    )

    clock.advance(wall=30.0, monotonic=30.0)
    assert tracker.observe_resume() is False

    clock.advance(wall=120.0, monotonic=1.0)
    assert tracker.observe_resume() is True

    clock.advance(wall=59.0, monotonic=59.0)
    assert tracker.observe_resume() is True

    clock.advance(wall=1.0, monotonic=1.0)
    assert tracker.observe_resume() is False


def test_host_sleep_tracker_ignores_small_clock_adjustment() -> None:
    clock = _Clock()
    tracker = HostSleepTracker(
        suspend_threshold_seconds=5.0,
        wall_clock=lambda: clock.wall,
        monotonic_clock=lambda: clock.monotonic,
    )

    clock.advance(wall=12.0, monotonic=10.0)

    assert tracker.observe_resume() is False
