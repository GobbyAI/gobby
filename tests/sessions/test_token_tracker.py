"""Tests for session token usage aggregation."""

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

        tracker = SessionTokenTracker(session_storage=mock_session_storage)

        assert tracker.session_storage is mock_session_storage


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
