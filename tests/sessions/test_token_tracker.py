"""Tests for gobby.sessions.token_tracker module.

Tests for the SessionTokenTracker class that aggregates usage from sessions
and provides budget tracking.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_session_storage():
    """Create a mock session storage."""
    storage = MagicMock()
    return storage


@pytest.fixture
def sample_sessions():
    """Create sample sessions with usage data."""
    now = datetime.now(UTC)

    # Create mock sessions with different timestamps
    sessions = [
        MagicMock(
            id="sess-1",
            usage_input_tokens=1000,
            usage_output_tokens=500,
            usage_total_cost_usd=0.05,
            model="claude-3-5-sonnet-20241022",
            created_at=(now - timedelta(hours=1)).isoformat(),
        ),
        MagicMock(
            id="sess-2",
            usage_input_tokens=2000,
            usage_output_tokens=1000,
            usage_total_cost_usd=0.10,
            model="claude-3-5-sonnet-20241022",
            created_at=(now - timedelta(hours=2)).isoformat(),
        ),
        MagicMock(
            id="sess-3",
            usage_input_tokens=5000,
            usage_output_tokens=2500,
            usage_total_cost_usd=0.25,
            model="gemini/gemini-2.0-flash-exp",
            created_at=(now - timedelta(days=2)).isoformat(),
        ),
    ]
    return sessions


class TestSessionTokenTrackerInit:
    """Tests for SessionTokenTracker initialization."""

    def test_init_with_storage(self, mock_session_storage) -> None:
        """Initialize with session storage."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_usd=10.0,
        )

        assert tracker.session_storage is mock_session_storage
        assert tracker.daily_budget_usd == 10.0

    def test_init_default_budget(self, mock_session_storage) -> None:
        """Initialize with default budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        tracker = SessionTokenTracker(session_storage=mock_session_storage)

        assert tracker.daily_budget_usd == 50.0  # Default budget


class TestGetUsageSummary:
    """Tests for get_usage_summary method."""

    def test_get_usage_summary_last_day(self, mock_session_storage, sample_sessions) -> None:
        """Get usage summary for last day."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        # Mock storage to return sessions from last day
        # Only sess-1 and sess-2 should be within last day
        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(session_storage=mock_session_storage)
        summary = tracker.get_usage_summary(days=1)

        assert summary["total_cost_usd"] == pytest.approx(0.15)  # 0.05 + 0.10
        assert summary["total_input_tokens"] == 3000  # 1000 + 2000
        assert summary["total_output_tokens"] == 1500  # 500 + 1000
        assert summary["session_count"] == 2

    def test_get_usage_summary_multiple_days(self, mock_session_storage, sample_sessions) -> None:
        """Get usage summary for multiple days."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        # Mock storage to return all sessions
        mock_session_storage.get_sessions_since.return_value = sample_sessions

        tracker = SessionTokenTracker(session_storage=mock_session_storage)
        summary = tracker.get_usage_summary(days=7)

        assert summary["total_cost_usd"] == pytest.approx(0.40)  # 0.05 + 0.10 + 0.25
        assert summary["total_input_tokens"] == 8000  # 1000 + 2000 + 5000
        assert summary["session_count"] == 3

    def test_get_usage_summary_by_model(self, mock_session_storage, sample_sessions) -> None:
        """Get usage summary broken down by model."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions

        tracker = SessionTokenTracker(session_storage=mock_session_storage)
        summary = tracker.get_usage_summary(days=7)

        assert "usage_by_model" in summary
        assert "claude-3-5-sonnet-20241022" in summary["usage_by_model"]
        assert "gemini/gemini-2.0-flash-exp" in summary["usage_by_model"]

        claude_usage = summary["usage_by_model"]["claude-3-5-sonnet-20241022"]
        assert claude_usage["cost"] == pytest.approx(0.15)  # 0.05 + 0.10
        assert claude_usage["sessions"] == 2

    def test_get_usage_summary_empty(self, mock_session_storage) -> None:
        """Get usage summary with no sessions."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = []

        tracker = SessionTokenTracker(session_storage=mock_session_storage)
        summary = tracker.get_usage_summary(days=1)

        assert summary["total_cost_usd"] == 0.0
        assert summary["session_count"] == 0


class TestGetBudgetStatus:
    """Tests for get_budget_status method."""

    def test_get_budget_status_under_budget(self, mock_session_storage, sample_sessions) -> None:
        """Get budget status when under budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        # Only return today's sessions (0.15 total cost)
        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_usd=10.0,
        )
        status = tracker.get_budget_status()

        assert status["daily_budget_usd"] == 10.0
        assert status["used_today_usd"] == pytest.approx(0.15)
        assert status["remaining_usd"] == pytest.approx(9.85)
        assert status["percentage_used"] == pytest.approx(1.5)  # 0.15/10 * 100
        assert status["over_budget"] is False

    def test_get_budget_status_over_budget(self, mock_session_storage, sample_sessions) -> None:
        """Get budget status when over budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_usd=0.10,  # Budget is only $0.10
        )
        status = tracker.get_budget_status()

        assert status["daily_budget_usd"] == 0.10
        assert status["used_today_usd"] == pytest.approx(0.15)
        assert status["remaining_usd"] == pytest.approx(-0.05)
        assert status["over_budget"] is True


class TestCanSpawnAgent:
    """Tests for can_spawn_agent method."""

    def test_can_spawn_agent_under_budget(self, mock_session_storage, sample_sessions) -> None:
        """Can spawn agent when under budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_usd=10.0,
        )
        can_spawn, reason = tracker.can_spawn_agent()

        assert can_spawn is True
        assert reason is None

    def test_cannot_spawn_agent_over_budget(self, mock_session_storage, sample_sessions) -> None:
        """Cannot spawn agent when over budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_usd=0.10,  # Budget is only $0.10
        )
        can_spawn, reason = tracker.can_spawn_agent()

        assert can_spawn is False
        assert "budget exceeded" in reason.lower()

    def test_can_spawn_agent_with_estimated_cost(
        self, mock_session_storage, sample_sessions
    ) -> None:
        """Cannot spawn agent when estimated cost would exceed budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_usd=0.20,  # Budget is $0.20, used is $0.15
        )

        # Without estimated cost - should be able to spawn
        can_spawn, _ = tracker.can_spawn_agent()
        assert can_spawn is True

        # With estimated cost that would exceed budget
        can_spawn, reason = tracker.can_spawn_agent(estimated_cost=0.10)
        assert can_spawn is False
        assert "exceed budget" in reason.lower()

    def test_can_spawn_agent_unlimited_budget(self, mock_session_storage) -> None:
        """Can always spawn agent when budget is unlimited (0)."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        # Return expensive sessions
        expensive_session = MagicMock(
            id="sess-expensive",
            usage_input_tokens=1000000,
            usage_output_tokens=500000,
            usage_total_cost_usd=100.0,  # $100 used
            model="claude-3-5-sonnet-20241022",
            created_at=datetime.now(UTC).isoformat(),
        )
        mock_session_storage.get_sessions_since.return_value = [expensive_session]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_usd=0.0,  # Unlimited budget
        )
        can_spawn, reason = tracker.can_spawn_agent()

        assert can_spawn is True
        assert reason is None
