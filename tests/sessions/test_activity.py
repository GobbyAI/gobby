"""Tests for generic session activity tracking."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from gobby.sessions import activity


@pytest.fixture(autouse=True)
def reset_activity() -> Iterator[None]:
    activity.reset_for_tests()
    yield
    activity.reset_for_tests()


def test_record_and_clear_session_activity() -> None:
    recorded_at = datetime.now(UTC)

    activity.record_session_activity("session-1", recorded_at)

    assert activity.last_session_activity("session-1") == recorded_at
    activity.clear_trackers("session-1")
    assert activity.last_session_activity("session-1") is None


def test_prune_trackers_removes_expired_activity() -> None:
    now = datetime.now(UTC)
    stale_at = now - timedelta(seconds=activity.SESSION_ACTIVITY_TTL_SECONDS + 1)
    activity.record_session_activity("stale-session", stale_at)

    activity.prune_trackers(now)

    assert activity.last_session_activity("stale-session") is None
