"""Tests for gobby.sessions.token_tracker module.

Tests for the SessionTokenTracker class that aggregates usage from sessions
and provides budget tracking based on token counts (not USD).
"""

from datetime import UTC, datetime, timedelta
from typing import Any
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

    sessions = [
        MagicMock(
            id="sess-1",
            usage_input_tokens=1000,
            usage_output_tokens=500,
            usage_cache_creation_tokens=None,
            usage_cache_read_tokens=None,
            model="claude-3-5-sonnet-20241022",
            source="claude",
            created_at=(now - timedelta(hours=1)).isoformat(),
        ),
        MagicMock(
            id="sess-2",
            usage_input_tokens=2000,
            usage_output_tokens=1000,
            usage_cache_creation_tokens=None,
            usage_cache_read_tokens=None,
            model="claude-3-5-sonnet-20241022",
            source="claude",
            created_at=(now - timedelta(hours=2)).isoformat(),
        ),
        MagicMock(
            id="sess-3",
            usage_input_tokens=5000,
            usage_output_tokens=2500,
            usage_cache_creation_tokens=None,
            usage_cache_read_tokens=None,
            model="gemini/gemini-2.0-flash-exp",
            source="gemini",
            created_at=(now - timedelta(days=2)).isoformat(),
        ),
    ]
    return sessions


class TestSessionTokenTrackerInit:
    """Tests for SessionTokenTracker initialization."""

    def test_init_with_storage(self, mock_session_storage: MagicMock) -> None:
        """Initialize with session storage."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_tokens=5_000_000,
        )

        assert tracker.session_storage is mock_session_storage
        assert tracker.daily_budget_tokens == 5_000_000

    def test_init_default_budget(self, mock_session_storage: MagicMock) -> None:
        """Initialize with default budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        tracker = SessionTokenTracker(session_storage=mock_session_storage)

        assert tracker.daily_budget_tokens == 10_000_000  # Default: 10M tokens


class TestGetUsageSummary:
    """Tests for get_usage_summary method."""

    def test_get_usage_summary_last_day(
        self, mock_session_storage: MagicMock, sample_sessions: list[Any]
    ) -> None:
        """Get usage summary for last day."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(session_storage=mock_session_storage)
        summary = tracker.get_usage_summary(days=1)

        assert summary["total_input_tokens"] == 3000  # 1000 + 2000
        assert summary["total_output_tokens"] == 1500  # 500 + 1000
        assert summary["session_count"] == 2

    def test_get_usage_summary_multiple_days(
        self, mock_session_storage: MagicMock, sample_sessions: list[Any]
    ) -> None:
        """Get usage summary for multiple days."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions

        tracker = SessionTokenTracker(session_storage=mock_session_storage)
        summary = tracker.get_usage_summary(days=7)

        assert summary["total_input_tokens"] == 8000  # 1000 + 2000 + 5000
        assert summary["session_count"] == 3

    def test_get_usage_summary_by_model(
        self, mock_session_storage: MagicMock, sample_sessions: list[Any]
    ) -> None:
        """Get usage summary broken down by model."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions

        tracker = SessionTokenTracker(session_storage=mock_session_storage)
        summary = tracker.get_usage_summary(days=7)

        assert "usage_by_model" in summary
        assert "claude-3-5-sonnet-20241022" in summary["usage_by_model"]
        assert "gemini/gemini-2.0-flash-exp" in summary["usage_by_model"]

        claude_usage = summary["usage_by_model"]["claude-3-5-sonnet-20241022"]
        assert claude_usage["input_tokens"] == 3000
        assert claude_usage["sessions"] == 2

    def test_get_usage_summary_by_source(
        self, mock_session_storage: MagicMock, sample_sessions: list[Any]
    ) -> None:
        """Get usage summary broken down by source (CLI adapter)."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions

        tracker = SessionTokenTracker(session_storage=mock_session_storage)
        summary = tracker.get_usage_summary(days=7)

        assert "usage_by_source" in summary
        assert "claude" in summary["usage_by_source"]
        assert "gemini" in summary["usage_by_source"]

        claude_usage = summary["usage_by_source"]["claude"]
        assert claude_usage["input_tokens"] == 3000  # 1000 + 2000
        assert claude_usage["output_tokens"] == 1500  # 500 + 1000
        assert claude_usage["sessions"] == 2

        gemini_usage = summary["usage_by_source"]["gemini"]
        assert gemini_usage["input_tokens"] == 5000
        assert gemini_usage["sessions"] == 1

    def test_get_usage_summary_passes_project_id(self, mock_session_storage: MagicMock) -> None:
        """Project ID is forwarded to storage layer."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = []

        tracker = SessionTokenTracker(session_storage=mock_session_storage)
        tracker.get_usage_summary(days=3, project_id="proj-123")

        call_args = mock_session_storage.get_sessions_since.call_args
        assert call_args.kwargs.get("project_id") == "proj-123"

    def test_get_usage_summary_empty(self, mock_session_storage: MagicMock) -> None:
        """Get usage summary with no sessions."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = []

        tracker = SessionTokenTracker(session_storage=mock_session_storage)
        summary = tracker.get_usage_summary(days=1)

        assert summary["total_input_tokens"] == 0
        assert summary["total_output_tokens"] == 0
        assert summary["session_count"] == 0
        assert summary["usage_by_source"] == {}
        assert summary["usage_by_model"] == {}


class TestGetBudgetStatus:
    """Tests for get_budget_status method."""

    def test_get_budget_status_under_budget(
        self, mock_session_storage: MagicMock, sample_sessions: list[Any]
    ) -> None:
        """Get budget status when under budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        # sess-1: 1500 tokens, sess-2: 3000 tokens = 4500 total
        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_tokens=10_000_000,
        )
        status = tracker.get_budget_status()

        assert status["daily_budget_tokens"] == 10_000_000
        assert status["used_today_tokens"] == 4500  # 1000+500 + 2000+1000
        assert status["remaining_tokens"] == 10_000_000 - 4500
        assert status["over_budget"] is False

    def test_get_budget_status_over_budget(
        self, mock_session_storage: MagicMock, sample_sessions: list[Any]
    ) -> None:
        """Get budget status when over budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_tokens=1000,  # Very small budget
        )
        status = tracker.get_budget_status()

        assert status["daily_budget_tokens"] == 1000
        assert status["used_today_tokens"] == 4500
        assert status["over_budget"] is True

    def test_get_budget_status_negative_budget_is_unlimited(
        self, mock_session_storage: MagicMock, sample_sessions: list[Any]
    ) -> None:
        """Negative daily_budget_tokens is treated as unlimited (same as 0)."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_tokens=-10,
        )
        status = tracker.get_budget_status()

        assert status["daily_budget_tokens"] == -10
        assert status["over_budget"] is False
        assert status["remaining_tokens"] == float("inf")


class TestCanSpawnAgent:
    """Tests for can_spawn_agent method."""

    def test_can_spawn_agent_under_budget(
        self, mock_session_storage: MagicMock, sample_sessions: list[Any]
    ) -> None:
        """Can spawn agent when under budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_tokens=10_000_000,
        )
        can_spawn, reason = tracker.can_spawn_agent()

        assert can_spawn is True
        assert reason is None

    def test_cannot_spawn_agent_over_budget(
        self, mock_session_storage: MagicMock, sample_sessions: list[Any]
    ) -> None:
        """Cannot spawn agent when over budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_tokens=1000,  # Very small budget
        )
        can_spawn, reason = tracker.can_spawn_agent()

        assert can_spawn is False
        assert reason is not None and "budget exceeded" in reason.lower()

    def test_can_spawn_agent_with_estimated_tokens(
        self, mock_session_storage: MagicMock, sample_sessions: list[Any]
    ) -> None:
        """Cannot spawn agent when estimated tokens would exceed budget."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        mock_session_storage.get_sessions_since.return_value = sample_sessions[:2]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_tokens=5000,  # Budget is 5000, used is 4500
        )

        # Without estimated tokens - should be able to spawn
        can_spawn, _ = tracker.can_spawn_agent()
        assert can_spawn is True

        # With estimated tokens that would exceed budget
        can_spawn, reason = tracker.can_spawn_agent(estimated_tokens=1000)
        assert can_spawn is False
        assert reason is not None and "exceed budget" in reason.lower()

    def test_can_spawn_agent_unlimited_budget(self, mock_session_storage: MagicMock) -> None:
        """Can always spawn agent when budget is unlimited (0)."""
        from gobby.sessions.token_tracker import SessionTokenTracker

        expensive_session = MagicMock(
            id="sess-expensive",
            usage_input_tokens=1_000_000,
            usage_output_tokens=500_000,
            usage_cache_creation_tokens=None,
            usage_cache_read_tokens=None,
            model="claude-3-5-sonnet-20241022",
            source="claude",
            created_at=datetime.now(UTC).isoformat(),
        )
        mock_session_storage.get_sessions_since.return_value = [expensive_session]

        tracker = SessionTokenTracker(
            session_storage=mock_session_storage,
            daily_budget_tokens=0,  # Unlimited budget
        )
        can_spawn, reason = tracker.can_spawn_agent()

        assert can_spawn is True
        assert reason is None
