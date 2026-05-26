"""Tests for SavingsTracker."""

import pytest

from gobby.savings.tracker import CHARS_PER_TOKEN, VALID_CATEGORIES, SavingsTracker
from gobby.storage.hub.protocol import HubDatabase


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Use the migrated PostgreSQL hub database fixture."""
    return temp_db


@pytest.fixture
def tracker(db: HubDatabase) -> SavingsTracker:
    return SavingsTracker(db=db)


pytestmark = pytest.mark.unit


class TestSavingsTracker:
    def test_record_chars(self, tracker: SavingsTracker) -> None:
        tracker.record(category="code_index", original_chars=10000, actual_chars=2000)
        summary = tracker.get_summary(days=1)
        assert summary["total_tokens_saved"] > 0
        assert summary["categories"]["code_index"]["event_count"] == 1

    def test_record_tokens(self, tracker: SavingsTracker) -> None:
        tracker.record_tokens(
            category="code_index",
            original_tokens=5000,
            actual_tokens=500,
        )
        summary = tracker.get_summary(days=1)
        assert summary["total_tokens_saved"] == 4500
        assert summary["categories"]["code_index"]["tokens_saved"] == 4500

    def test_record_with_session_and_project(self, tracker: SavingsTracker) -> None:
        tracker.record_tokens(
            category="code_index",
            original_tokens=15000,
            actual_tokens=3000,
            session_id="sess-1",
            project_id="proj-1",
        )
        # Filter by project
        summary = tracker.get_summary(days=1, project_id="proj-1")
        assert summary["total_tokens_saved"] == 12000

        # Different project should be empty
        summary2 = tracker.get_summary(days=1, project_id="proj-other")
        assert summary2["total_tokens_saved"] == 0

    def test_multiple_categories(self, tracker: SavingsTracker) -> None:
        tracker.record_tokens(category="code_index", original_tokens=5000, actual_tokens=500)
        tracker.record_tokens(category="discovery", original_tokens=8000, actual_tokens=1000)

        summary = tracker.get_summary(days=1)
        assert len(summary["categories"]) == 2
        assert summary["total_tokens_saved"] == (4500 + 7000)
        assert summary["total_events"] == 2

    def test_chars_to_tokens_conversion(self, tracker: SavingsTracker) -> None:
        tracker.record(category="code_index", original_chars=3700, actual_chars=370)
        summary = tracker.get_summary(days=1)
        cat = summary["categories"]["code_index"]
        assert cat["original_tokens"] == int(3700 / CHARS_PER_TOKEN)
        assert cat["actual_tokens"] == int(370 / CHARS_PER_TOKEN)

    def test_empty_summary(self, tracker: SavingsTracker) -> None:
        summary = tracker.get_summary(days=1)
        assert summary["total_tokens_saved"] == 0
        assert summary["total_events"] == 0
        assert summary["categories"] == {}

    def test_negative_savings_clamped(self, tracker: SavingsTracker) -> None:
        """If actual > original, tokens_saved should be 0."""
        tracker.record_tokens(category="code_index", original_tokens=100, actual_tokens=200)
        summary = tracker.get_summary(days=1)
        assert summary["categories"]["code_index"]["tokens_saved"] == 0

    def test_invalid_category_rejected(self, tracker: SavingsTracker) -> None:
        """Invalid categories are silently dropped — never appear in summaries."""
        tracker.record_tokens(category="memory", original_tokens=8000, actual_tokens=0)
        tracker.record(category="handoff", original_chars=55500, actual_chars=1000)
        tracker.record_tokens(category="code_index", original_tokens=1000, actual_tokens=200)

        summary = tracker.get_summary(days=1)
        assert list(summary["categories"].keys()) == ["code_index"]
        assert summary["total_tokens_saved"] == 800
        assert summary["total_events"] == 1

    def test_valid_categories_constant(self) -> None:
        """VALID_CATEGORIES contains exactly the expected set."""
        assert VALID_CATEGORIES == {"code_index", "discovery", "compression"}
