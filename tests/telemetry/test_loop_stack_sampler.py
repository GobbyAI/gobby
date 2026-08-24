"""Name the Python code running on the loop thread while it refuses to yield.

``sample(1)`` collapses every Python frame into ``_PyEval_EvalFrameDefault``,
and across 35 seconds of a real 68s daemon stall its loop-thread time was
spread thin over dict operations, isinstance checks and attribute lookups --
proof that a lot of ordinary Python was running, with no hot syscall to blame.
py-spy would name the frames but needs root on macOS.

A sampler thread inside the process can read that stack directly: the
interpreter releases the GIL every few milliseconds, so a second thread gets
scheduled even while the loop thread is busy, and ``sys._current_frames()``
hands it the loop thread's real Python frames (#20841).
"""

from __future__ import annotations

import threading
import time

import pytest

from gobby.telemetry.loop_stack_sampler import LoopStackSampler

pytestmark = pytest.mark.unit

SAMPLE_INTERVAL_SECONDS = 0.002
# Long enough to collect many samples, short enough to keep the suite quick.
BURN_SECONDS = 0.4


def burn_inside_a_named_helper(deadline: float) -> int:
    """Spin under a recognisable name so the sampler has something to find."""
    spins = 0
    while time.monotonic() < deadline:
        spins += 1
    return spins


def test_the_sampler_names_the_function_running_on_the_watched_thread() -> None:
    """The hottest stack must name the helper doing the work, and its caller."""
    sampler = LoopStackSampler(
        threading.get_ident(),
        interval_seconds=SAMPLE_INTERVAL_SECONDS,
    )
    sampler.start()
    try:
        burn_inside_a_named_helper(time.monotonic() + BURN_SECONDS)
    finally:
        sampler.stop()

    hottest = sampler.drain()
    assert hottest, "a sampler that collects nothing cannot name anything"
    top_stack, count = hottest[0]
    assert count > 0
    assert "burn_inside_a_named_helper" in top_stack, (
        f"the hottest stack must name the burning function; got {top_stack!r}"
    )
    assert "test_the_sampler_names_the_function_running_on_the_watched_thread" in top_stack, (
        f"and keep the caller that led there; got {top_stack!r}"
    )


def test_draining_clears_so_each_stall_reports_only_its_own_samples() -> None:
    """Samples belong to the window they were taken in, not to every later one."""
    sampler = LoopStackSampler(
        threading.get_ident(),
        interval_seconds=SAMPLE_INTERVAL_SECONDS,
    )
    sampler.start()
    try:
        burn_inside_a_named_helper(time.monotonic() + BURN_SECONDS)
        first = sampler.drain()
        second = sampler.drain()
    finally:
        sampler.stop()

    assert first, "the first drain must return the window's samples"
    assert second == [], f"a drained sampler must start empty; got {second!r}"


def test_a_stopped_sampler_collects_nothing_further() -> None:
    """Stopping must actually retire the thread, not just mute it."""
    sampler = LoopStackSampler(
        threading.get_ident(),
        interval_seconds=SAMPLE_INTERVAL_SECONDS,
    )
    sampler.start()
    burn_inside_a_named_helper(time.monotonic() + 0.05)
    sampler.stop()
    sampler.drain()

    burn_inside_a_named_helper(time.monotonic() + 0.1)
    assert sampler.drain() == []
    assert not sampler.is_running()


def test_watching_a_thread_that_does_not_exist_is_harmless() -> None:
    """A diagnostic must never be the thing that breaks the daemon."""
    sampler = LoopStackSampler(-1, interval_seconds=SAMPLE_INTERVAL_SECONDS)
    sampler.start()
    try:
        burn_inside_a_named_helper(time.monotonic() + 0.05)
    finally:
        sampler.stop()
    assert sampler.drain() == []
