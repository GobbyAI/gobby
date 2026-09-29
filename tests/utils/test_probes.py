"""ProbeBatch runs independent probes concurrently under one shared deadline."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from gobby.utils.probes import ProbeBatch

pytestmark = pytest.mark.unit


@pytest.fixture
def release() -> Iterator[threading.Event]:
    gate = threading.Event()
    yield gate
    gate.set()


def test_collect_returns_at_the_deadline_and_names_the_slow_probes(
    release: threading.Event,
) -> None:
    started = time.monotonic()
    batch = ProbeBatch(
        {
            "fast": lambda: "ready",
            "slow_a": lambda: release.wait(10),
            "slow_b": lambda: release.wait(10),
        },
        deadline=started + 0.2,
    )

    results = batch.collect()

    assert time.monotonic() - started < 1.5
    assert results.values == {"fast": "ready"}
    assert results.timed_out == ("slow_a", "slow_b")


def test_probes_run_concurrently_without_a_deadline() -> None:
    # The barrier only opens when all five probes are running at the same time.
    barrier = threading.Barrier(5, timeout=5)
    batch = ProbeBatch(
        {f"probe_{index}": barrier.wait for index in range(5)},
        deadline=None,
    )

    results = batch.collect()

    assert sorted(results.values) == [f"probe_{index}" for index in range(5)]
    assert sorted(results.values.values()) == [0, 1, 2, 3, 4]
    assert results.timed_out == ()


def test_a_failing_probe_propagates_its_exception() -> None:
    def broken() -> str:
        raise RuntimeError("probe exploded")

    batch = ProbeBatch({"broken": broken}, deadline=None)

    with pytest.raises(RuntimeError, match="probe exploded"):
        batch.collect()
