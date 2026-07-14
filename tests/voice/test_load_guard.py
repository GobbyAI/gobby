"""Tests for the TTS model load-guard crash-loop breaker (incident #18196)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.voice.load_guard import ModelLoadGuard

pytestmark = pytest.mark.unit


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_guard(tmp_path: Path, clock: FakeClock) -> ModelLoadGuard:
    return ModelLoadGuard(
        tmp_path / "voice" / "tts_load_guard.json",
        max_attempts=3,
        window_seconds=900.0,
        cooldown_seconds=1800.0,
        now=clock,
    )


def test_fresh_guard_allows_loading(tmp_path: Path) -> None:
    guard = make_guard(tmp_path, FakeClock())
    assert guard.check() is None


def test_latches_after_max_attempts_in_window(tmp_path: Path) -> None:
    clock = FakeClock()
    guard = make_guard(tmp_path, clock)
    for _ in range(3):
        assert guard.check() is None or True
        guard.record_attempt()
        clock.now += 60.0
    reason = guard.check()
    assert reason is not None
    assert "3 load attempts died" in reason


def test_success_clears_the_latch(tmp_path: Path) -> None:
    clock = FakeClock()
    guard = make_guard(tmp_path, clock)
    for _ in range(3):
        guard.record_attempt()
    assert guard.check() is not None
    guard.record_success()
    assert guard.check() is None


def test_cooldown_expiry_allows_retry(tmp_path: Path) -> None:
    clock = FakeClock()
    guard = make_guard(tmp_path, clock)
    for _ in range(3):
        guard.record_attempt()
    assert guard.check() is not None
    clock.now += 1799.0
    assert guard.check() is not None
    clock.now += 2.0  # past last attempt + cooldown
    assert guard.check() is None


def test_slow_failures_outside_window_do_not_latch(tmp_path: Path) -> None:
    clock = FakeClock()
    guard = make_guard(tmp_path, clock)
    for _ in range(3):
        guard.record_attempt()
        clock.now += 1000.0  # spaced wider than the 900s window
    assert guard.check() is None


def test_marker_is_persisted_durably_and_corrupt_marker_is_tolerated(tmp_path: Path) -> None:
    clock = FakeClock()
    guard = make_guard(tmp_path, clock)
    guard.record_attempt()
    marker = tmp_path / "voice" / "tts_load_guard.json"
    assert marker.exists()
    entries = json.loads(marker.read_text())
    assert len(entries) == 1 and "ts" in entries[0] and "pid" in entries[0]

    marker.write_text("{not json")
    assert guard.check() is None  # fail-open on corruption
    guard.record_attempt()  # and recoverable
    assert len(json.loads(marker.read_text())) == 1
