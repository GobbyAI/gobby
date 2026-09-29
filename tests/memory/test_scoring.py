"""Unit tests for memory scoring helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gobby.memory.scoring import temporal_decay

pytestmark = pytest.mark.unit


class TestTemporalDecay:
    """Tests for the temporal_decay half-life function."""

    def test_recent_memory_returns_near_one(self) -> None:
        """A memory updated just now should have decay factor ~1.0."""
        now = datetime(2026, 4, 1, tzinfo=UTC)
        factor = temporal_decay(now.isoformat(), half_life_days=30.0, now=now)
        assert factor == pytest.approx(1.0)

    def test_one_half_life_returns_half(self) -> None:
        """A memory exactly one half-life old should have factor 0.5."""
        now = datetime(2026, 4, 1, tzinfo=UTC)
        thirty_days_ago = now - timedelta(days=30)
        factor = temporal_decay(thirty_days_ago.isoformat(), half_life_days=30.0, now=now)
        assert factor == pytest.approx(0.5)

    def test_two_half_lives_returns_quarter(self) -> None:
        """A memory two half-lives old should have factor 0.25."""
        now = datetime(2026, 4, 1, tzinfo=UTC)
        sixty_days_ago = now - timedelta(days=60)
        factor = temporal_decay(sixty_days_ago.isoformat(), half_life_days=30.0, now=now)
        assert factor == pytest.approx(0.25)

    def test_one_week_gentle_decay(self) -> None:
        """A one-week-old memory should still rank well (~0.85)."""
        now = datetime(2026, 4, 1, tzinfo=UTC)
        one_week_ago = now - timedelta(days=7)
        factor = temporal_decay(one_week_ago.isoformat(), half_life_days=30.0, now=now)
        assert factor > 0.8

    def test_disabled_when_zero(self) -> None:
        """Half-life of 0 disables decay (returns 1.0)."""
        old = datetime(2020, 1, 1, tzinfo=UTC)
        factor = temporal_decay(old.isoformat(), half_life_days=0.0)
        assert factor == 1.0

    def test_disabled_when_negative(self) -> None:
        """Negative half-life disables decay (returns 1.0)."""
        old = datetime(2020, 1, 1, tzinfo=UTC)
        factor = temporal_decay(old.isoformat(), half_life_days=-5.0)
        assert factor == 1.0

    def test_invalid_timestamp_returns_one(self) -> None:
        """Invalid updated_at should not crash; returns 1.0."""
        factor = temporal_decay("not-a-date", half_life_days=30.0)
        assert factor == 1.0

    def test_future_timestamp_returns_one(self) -> None:
        """A future timestamp should return 1.0 (clamped to 0 age)."""
        now = datetime(2026, 4, 1, tzinfo=UTC)
        future = now + timedelta(days=10)
        factor = temporal_decay(future.isoformat(), half_life_days=30.0, now=now)
        assert factor == pytest.approx(1.0)

    def test_naive_timestamp_treated_as_utc(self) -> None:
        """A naive (no timezone) timestamp should be treated as UTC."""
        now = datetime(2026, 4, 1, tzinfo=UTC)
        thirty_days_ago = (now - timedelta(days=30)).replace(tzinfo=None)
        factor = temporal_decay(thirty_days_ago.isoformat(), half_life_days=30.0, now=now)
        assert factor == pytest.approx(0.5)

    def test_very_old_memory_near_zero(self) -> None:
        """A memory from years ago should have a very low factor."""
        now = datetime(2026, 4, 1, tzinfo=UTC)
        years_ago = now - timedelta(days=365)
        factor = temporal_decay(years_ago.isoformat(), half_life_days=30.0, now=now)
        assert factor < 0.01


def test_recency_anchor_prefers_later_access() -> None:
    """The decay anchor is the later of the last update and the last fetch."""
    from gobby.memory.scoring import recency_anchor

    updated = datetime(2026, 1, 1, tzinfo=UTC)
    fetched = datetime(2026, 3, 1, tzinfo=UTC)

    assert recency_anchor(updated.isoformat(), fetched) == fetched
    assert recency_anchor(fetched.isoformat(), updated) == fetched
    assert recency_anchor(updated.isoformat(), None) == updated
    assert recency_anchor(None, fetched) == fetched
    assert recency_anchor(None, None) is None
