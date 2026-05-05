"""Tests for gobby.agents.loop_tracker module."""

from __future__ import annotations

import pytest

from gobby.agents.loop_tracker import LoopTracker

pytestmark = pytest.mark.unit


class TestRecordDismissal:
    """Tests for LoopTracker.record_dismissal()."""

    def setup_method(self) -> None:
        self.tracker = LoopTracker(threshold=3)

    def test_first_dismissal_returns_one(self) -> None:
        assert self.tracker.record_dismissal("run-1") == 1

    def test_increments_count(self) -> None:
        self.tracker.record_dismissal("run-1")
        self.tracker.record_dismissal("run-1")
        assert self.tracker.record_dismissal("run-1") == 3

    def test_independent_per_run(self) -> None:
        self.tracker.record_dismissal("run-1")
        self.tracker.record_dismissal("run-1")
        assert self.tracker.record_dismissal("run-2") == 1
        assert self.tracker.get_count("run-1") == 2


class TestShouldEscalate:
    """Tests for LoopTracker.should_escalate()."""

    def setup_method(self) -> None:
        self.tracker = LoopTracker(threshold=3)

    def test_false_below_threshold(self) -> None:
        self.tracker.record_dismissal("run-1")
        self.tracker.record_dismissal("run-1")
        assert not self.tracker.should_escalate("run-1")

    def test_true_at_threshold(self) -> None:
        for _ in range(3):
            self.tracker.record_dismissal("run-1")
        assert self.tracker.should_escalate("run-1")

    def test_true_above_threshold(self) -> None:
        for _ in range(5):
            self.tracker.record_dismissal("run-1")
        assert self.tracker.should_escalate("run-1")

    def test_false_for_unknown_run(self) -> None:
        assert not self.tracker.should_escalate("unknown")

    def test_custom_threshold(self) -> None:
        tracker = LoopTracker(threshold=1)
        tracker.record_dismissal("run-1")
        assert tracker.should_escalate("run-1")


class TestGetCount:
    """Tests for LoopTracker.get_count()."""

    def setup_method(self) -> None:
        self.tracker = LoopTracker()

    def test_zero_for_unknown(self) -> None:
        assert self.tracker.get_count("unknown") == 0

    def test_returns_current_count(self) -> None:
        self.tracker.record_dismissal("run-1")
        self.tracker.record_dismissal("run-1")
        assert self.tracker.get_count("run-1") == 2


class TestClear:
    """Tests for LoopTracker.clear()."""

    def setup_method(self) -> None:
        self.tracker = LoopTracker(threshold=3)

    def test_clears_count(self) -> None:
        for _ in range(3):
            self.tracker.record_dismissal("run-1")
        self.tracker.clear("run-1")
        assert self.tracker.get_count("run-1") == 0
        assert not self.tracker.should_escalate("run-1")

    def test_clear_unknown_is_noop(self) -> None:
        result = self.tracker.clear("unknown")
        assert result is None
        assert self.tracker.get_count("unknown") == 0

    def test_clear_does_not_affect_other_runs(self) -> None:
        self.tracker.record_dismissal("run-1")
        self.tracker.record_dismissal("run-2")
        self.tracker.clear("run-1")
        assert self.tracker.get_count("run-2") == 1


class TestThresholdProperty:
    """Tests for LoopTracker.threshold property."""

    def test_default_threshold(self) -> None:
        assert LoopTracker().threshold == 3

    def test_custom_threshold(self) -> None:
        assert LoopTracker(threshold=5).threshold == 5

    def test_zero_threshold_raises(self) -> None:
        with pytest.raises(ValueError, match="threshold must be >= 1"):
            LoopTracker(threshold=0)

    def test_negative_threshold_raises(self) -> None:
        with pytest.raises(ValueError, match="threshold must be >= 1"):
            LoopTracker(threshold=-1)

    def test_large_threshold_accepted(self) -> None:
        assert LoopTracker(threshold=1_000_000).threshold == 1_000_000
