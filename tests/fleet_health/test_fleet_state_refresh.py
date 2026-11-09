"""A healthy fleet state stays fresh for as long as a test holds it."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from gobby.fleet_health.state import STALE_AFTER_SECONDS, read_fleet_state
from tests.fixtures import fleet_state as fixture_state
from tests.fixtures.fleet_state import (
    healthy_fleet_document,
    keep_fleet_state_fresh,
    publish_fleet_document,
    refresh_healthy_fleet_state,
    remove_fleet_state,
    write_healthy_fleet_state,
)

NOW = 1_800_000_000.0
POLL_SECONDS = 2.0


class FakeClock:
    """A wall clock the test moves forward past the gate's stale window."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def wait_for_state(path: Path, clock: FakeClock, state: str) -> str:
    deadline = time.monotonic() + POLL_SECONDS
    current = read_fleet_state(path, clock.now).state
    while current != state and time.monotonic() < deadline:
        time.sleep(0.01)
        current = read_fleet_state(path, clock.now).state
    return current


def test_state_stays_fresh_for_a_test_longer_than_the_stale_window(tmp_path: Path) -> None:
    path = tmp_path / "fleet-health.json"
    clock = FakeClock(NOW)
    write_healthy_fleet_state(path, sampled_at=NOW)

    with keep_fleet_state_fresh(path, interval_seconds=0.01, clock=clock):
        clock.now = NOW + 2 * STALE_AFTER_SECONDS
        assert wait_for_state(path, clock, "healthy") == "healthy"


def test_a_state_another_writer_replaces_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "fleet-health.json"
    healthy = write_healthy_fleet_state(path, sampled_at=NOW)
    degraded = healthy_fleet_document(NOW) | {
        "state": "degraded",
        "reasons": ["load_per_core:2.6"],
    }
    publish_fleet_document(path, degraded)

    assert refresh_healthy_fleet_state(path, last=healthy, sampled_at=NOW + 1) is None
    assert read_fleet_state(path, NOW).state == "degraded"


def run_at_the_refresh_publish(
    monkeypatch: pytest.MonkeyPatch, action: Callable[[], object]
) -> list[threading.Thread]:
    """Run action on a thread at the refresher's publish; the thread must wait for the lock."""
    real_publish = fixture_state._publish
    threads: list[threading.Thread] = []

    def publish_once_the_action_waits(target: Path, payload: bytes) -> None:
        # The first publish is the refresher's, between its check and its replace.
        if threads:
            real_publish(target, payload)
            return
        thread = threading.Thread(target=action)
        threads.append(thread)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive(), "a state writer must wait for the refresh holding the lock"
        real_publish(target, payload)

    monkeypatch.setattr(fixture_state, "_publish", publish_once_the_action_waits)
    return threads


def test_a_negative_write_cannot_land_between_the_refresh_check_and_its_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fleet-health.json"
    healthy = write_healthy_fleet_state(path, sampled_at=NOW)
    degraded = healthy_fleet_document(NOW) | {
        "state": "degraded",
        "reasons": ["load_per_core:2.6"],
    }
    writers = run_at_the_refresh_publish(
        monkeypatch, lambda: publish_fleet_document(path, degraded)
    )

    assert refresh_healthy_fleet_state(path, last=healthy, sampled_at=NOW + 1) is not None
    writers[0].join(timeout=5)
    assert read_fleet_state(path, NOW).state == "degraded"


def test_a_removal_cannot_land_between_the_refresh_check_and_its_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fleet-health.json"
    healthy = write_healthy_fleet_state(path, sampled_at=NOW)
    removers = run_at_the_refresh_publish(monkeypatch, lambda: remove_fleet_state(path))

    assert refresh_healthy_fleet_state(path, last=healthy, sampled_at=NOW + 1) is not None
    removers[0].join(timeout=5)
    assert not path.exists()
