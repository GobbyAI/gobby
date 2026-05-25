"""Comprehensive tests for autonomous session management modules.

Tests cover:
- ProgressTracker: progress event recording and stagnation detection
- StopRegistry: stop signal management and lifecycle
- StuckDetector: multi-layer stuck detection (task loops, progress stagnation, tool loops)
"""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from gobby.autonomous.progress_tracker import (
    HIGH_VALUE_PROGRESS,
    MEANINGFUL_TOOLS,
    ProgressEvent,
    ProgressTracker,
    ProgressType,
)
from gobby.autonomous.stop_registry import StopRegistry, StopSignal
from gobby.autonomous.stuck_detector import (
    StuckDetectionResult,
    StuckDetector,
    TaskSelectionEvent,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def test_db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    """Use the migrated PostgreSQL hub database fixture."""
    yield temp_db


@pytest.fixture
def project_manager(test_db: HubDatabase) -> LocalProjectManager:
    """Create a project manager."""
    return LocalProjectManager(test_db)


@pytest.fixture
def session_manager(test_db: HubDatabase) -> SessionManager:
    """Create a session manager."""
    return SessionManager(test_db)


@pytest.fixture
def test_project(project_manager: LocalProjectManager) -> dict:
    """Create a test project for session tests."""
    project = project_manager.create(
        name="test-project",
        repo_path="/tmp/test-autonomous",
    )
    return project.to_dict()


@pytest.fixture
def session_id(session_manager: SessionManager, test_project: dict) -> str:
    """Create a test session and return its ID."""
    session = session_manager.register(
        external_id="ext-test-session-123",
        machine_id="test-machine",
        source="claude",
        project_id=test_project["id"],
    )
    return session.id


@pytest.fixture
def progress_tracker(test_db: HubDatabase) -> ProgressTracker:
    """Create a ProgressTracker instance."""
    return ProgressTracker(test_db)


@pytest.fixture
def stop_registry(test_db: HubDatabase) -> StopRegistry:
    """Create a StopRegistry instance."""
    return StopRegistry(test_db)


@pytest.fixture
def stuck_detector(test_db: HubDatabase, progress_tracker: ProgressTracker) -> StuckDetector:
    """Create a StuckDetector instance with progress tracker."""
    return StuckDetector(test_db, progress_tracker=progress_tracker)


def create_session(
    session_manager: SessionManager,
    project_id: str,
    external_id: str,
) -> str:
    """Helper to create additional test sessions."""
    session = session_manager.register(
        external_id=external_id,
        machine_id="test-machine",
        source="claude",
        project_id=project_id,
    )
    return session.id


# ==============================================================================
# ProgressType and Constants Tests
# ==============================================================================


class TestProgressTypeConstants:
    """Tests for progress type definitions and constants."""

    def test_progress_type_values(self) -> None:
        """Test that all progress types have expected string values."""
        assert ProgressType.TOOL_CALL.value == "tool_call"
        assert ProgressType.FILE_MODIFIED.value == "file_modified"
        assert ProgressType.FILE_READ.value == "file_read"
        assert ProgressType.TASK_STARTED.value == "task_started"
        assert ProgressType.TASK_COMPLETED.value == "task_completed"
        assert ProgressType.TEST_PASSED.value == "test_passed"
        assert ProgressType.TEST_FAILED.value == "test_failed"
        assert ProgressType.BUILD_SUCCEEDED.value == "build_succeeded"
        assert ProgressType.BUILD_FAILED.value == "build_failed"
        assert ProgressType.COMMIT_CREATED.value == "commit_created"
        assert ProgressType.ERROR_OCCURRED.value == "error_occurred"

    def test_meaningful_tools_mapping(self) -> None:
        """Test MEANINGFUL_TOOLS maps tool names to progress types."""
        assert MEANINGFUL_TOOLS["Edit"] == ProgressType.FILE_MODIFIED
        assert MEANINGFUL_TOOLS["Write"] == ProgressType.FILE_MODIFIED
        assert MEANINGFUL_TOOLS["NotebookEdit"] == ProgressType.FILE_MODIFIED
        assert MEANINGFUL_TOOLS["Bash"] == ProgressType.TOOL_CALL
        assert MEANINGFUL_TOOLS["Read"] == ProgressType.FILE_READ
        assert MEANINGFUL_TOOLS["Glob"] == ProgressType.FILE_READ
        assert MEANINGFUL_TOOLS["Grep"] == ProgressType.FILE_READ

    def test_high_value_progress_types(self) -> None:
        """Test HIGH_VALUE_PROGRESS contains expected types."""
        assert ProgressType.FILE_MODIFIED in HIGH_VALUE_PROGRESS
        assert ProgressType.TASK_COMPLETED in HIGH_VALUE_PROGRESS
        assert ProgressType.COMMIT_CREATED in HIGH_VALUE_PROGRESS
        assert ProgressType.TEST_PASSED in HIGH_VALUE_PROGRESS
        assert ProgressType.BUILD_SUCCEEDED in HIGH_VALUE_PROGRESS
        # Low-value types should not be in set
        assert ProgressType.FILE_READ not in HIGH_VALUE_PROGRESS
        assert ProgressType.TOOL_CALL not in HIGH_VALUE_PROGRESS


# ==============================================================================
# ProgressEvent Tests
# ==============================================================================


class TestProgressEvent:
    """Tests for ProgressEvent dataclass."""

    def test_create_progress_event(self, session_id: str) -> None:
        """Test creating a basic progress event."""
        now = datetime.now(UTC)
        event = ProgressEvent(
            session_id=session_id,
            progress_type=ProgressType.FILE_MODIFIED,
            timestamp=now,
            tool_name="Edit",
            details={"file": "test.py"},
        )

        assert event.session_id == session_id
        assert event.progress_type == ProgressType.FILE_MODIFIED
        assert event.timestamp == now
        assert event.tool_name == "Edit"
        assert event.details == {"file": "test.py"}

    def test_is_high_value_for_high_value_types(self, session_id: str) -> None:
        """Test is_high_value returns True for high-value progress types."""
        now = datetime.now(UTC)

        for progress_type in HIGH_VALUE_PROGRESS:
            event = ProgressEvent(
                session_id=session_id,
                progress_type=progress_type,
                timestamp=now,
            )
            assert event.is_high_value is True, f"{progress_type} should be high value"

    def test_is_high_value_for_low_value_types(self, session_id: str) -> None:
        """Test is_high_value returns False for low-value progress types."""
        now = datetime.now(UTC)
        low_value_types = [
            ProgressType.TOOL_CALL,
            ProgressType.FILE_READ,
            ProgressType.TASK_STARTED,
            ProgressType.TEST_FAILED,
            ProgressType.BUILD_FAILED,
            ProgressType.ERROR_OCCURRED,
        ]

        for progress_type in low_value_types:
            event = ProgressEvent(
                session_id=session_id,
                progress_type=progress_type,
                timestamp=now,
            )
            assert event.is_high_value is False, f"{progress_type} should not be high value"

    def test_default_details_is_empty_dict(self, session_id: str) -> None:
        """Test that details defaults to empty dict."""
        event = ProgressEvent(
            session_id=session_id,
            progress_type=ProgressType.TOOL_CALL,
            timestamp=datetime.now(UTC),
        )
        assert event.details == {}


# ==============================================================================
# ProgressTracker Tests
# ==============================================================================


class TestProgressTracker:
    """Tests for ProgressTracker class."""

    def test_init_with_defaults(self, test_db: HubDatabase) -> None:
        """Test initialization with default thresholds."""
        tracker = ProgressTracker(test_db)
        assert tracker.stagnation_threshold == ProgressTracker.DEFAULT_STAGNATION_THRESHOLD
        assert tracker.max_low_value_events == ProgressTracker.DEFAULT_MAX_LOW_VALUE_EVENTS

    def test_init_with_custom_thresholds(self, test_db: HubDatabase) -> None:
        """Test initialization with custom thresholds."""
        tracker = ProgressTracker(
            test_db,
            stagnation_threshold=300.0,
            max_low_value_events=25,
        )
        assert tracker.stagnation_threshold == 300.0
        assert tracker.max_low_value_events == 25

    def test_record_event_basic(self, progress_tracker: ProgressTracker, session_id: str) -> None:
        """Test recording a basic progress event."""
        event = progress_tracker.record_event(
            session_id=session_id,
            progress_type=ProgressType.FILE_MODIFIED,
            tool_name="Edit",
            details={"file": "test.py"},
        )

        assert event.session_id == session_id
        assert event.progress_type == ProgressType.FILE_MODIFIED
        assert event.tool_name == "Edit"
        assert event.timestamp is not None

    def test_record_event_persists_to_database(
        self, progress_tracker: ProgressTracker, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test that recorded events are persisted to database."""
        progress_tracker.record_event(
            session_id=session_id,
            progress_type=ProgressType.FILE_MODIFIED,
            tool_name="Edit",
        )

        row = test_db.fetchone(
            "SELECT * FROM loop_progress WHERE session_id = ?",
            (session_id,),
        )

        assert row is not None
        assert row["session_id"] == session_id
        assert row["progress_type"] == "file_modified"
        assert row["tool_name"] == "Edit"
        assert row["is_high_value"] == 1  # FILE_MODIFIED is high value

    def test_record_event_sets_is_high_value_correctly(
        self, progress_tracker: ProgressTracker, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test that is_high_value flag is set correctly in database."""
        # Record high-value event
        progress_tracker.record_event(
            session_id=session_id,
            progress_type=ProgressType.FILE_MODIFIED,
        )

        # Record low-value event
        progress_tracker.record_event(
            session_id=session_id,
            progress_type=ProgressType.FILE_READ,
        )

        rows = test_db.fetchall(
            "SELECT progress_type, is_high_value FROM loop_progress WHERE session_id = ? ORDER BY id",
            (session_id,),
        )

        assert len(rows) == 2
        assert rows[0]["is_high_value"] == 1  # FILE_MODIFIED
        assert rows[1]["is_high_value"] == 0  # FILE_READ


class TestProgressTrackerToolCall:
    """Tests for ProgressTracker.record_tool_call method."""

    def test_record_tool_call_for_edit(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test recording Edit tool call."""
        event = progress_tracker.record_tool_call(
            session_id=session_id,
            tool_name="Edit",
            tool_args={"file_path": "/test.py", "old_string": "a", "new_string": "b"},
        )

        assert event is not None
        assert event.progress_type == ProgressType.FILE_MODIFIED
        assert event.tool_name == "Edit"

    def test_record_tool_call_for_read(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test recording Read tool call."""
        event = progress_tracker.record_tool_call(
            session_id=session_id,
            tool_name="Read",
            tool_args={"file_path": "/test.py"},
        )

        assert event is not None
        assert event.progress_type == ProgressType.FILE_READ
        assert event.is_high_value is False

    def test_record_tool_call_for_bash_test_pass(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test recording Bash tool with passing tests."""
        event = progress_tracker.record_tool_call(
            session_id=session_id,
            tool_name="Bash",
            tool_args={"command": "pytest tests/"},
            tool_result="5 passed in 1.23s",
        )

        assert event is not None
        assert event.progress_type == ProgressType.TEST_PASSED
        assert event.is_high_value is True

    def test_record_tool_call_for_exec_command_test_pass(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Shell aliases should use the same progress heuristics as Bash."""
        event = progress_tracker.record_tool_call(
            session_id=session_id,
            tool_name="exec_command",
            tool_args={"command": "pytest tests/"},
            tool_result="5 passed in 1.23s",
        )

        assert event is not None
        assert event.progress_type == ProgressType.TEST_PASSED
        assert event.is_high_value is True

    def test_record_tool_call_for_bash_test_fail(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test recording Bash tool with failing tests."""
        event = progress_tracker.record_tool_call(
            session_id=session_id,
            tool_name="Bash",
            tool_args={"command": "npm test"},
            tool_result="FAILED: 2 tests failed",
        )

        assert event is not None
        assert event.progress_type == ProgressType.TEST_FAILED
        assert event.is_high_value is False

    def test_record_tool_call_for_bash_build_success(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test recording Bash tool with successful build."""
        event = progress_tracker.record_tool_call(
            session_id=session_id,
            tool_name="Bash",
            tool_args={"command": "npm run build"},
            tool_result="Build completed successfully",
        )

        assert event is not None
        assert event.progress_type == ProgressType.BUILD_SUCCEEDED
        assert event.is_high_value is True

    def test_record_tool_call_for_bash_build_failed(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test recording Bash tool with failed build."""
        event = progress_tracker.record_tool_call(
            session_id=session_id,
            tool_name="Bash",
            tool_args={"command": "cargo build"},
            tool_result="error[E0308]: mismatched types",
        )

        assert event is not None
        assert event.progress_type == ProgressType.BUILD_FAILED
        assert event.is_high_value is False

    def test_record_tool_call_for_git_commit(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test recording git commit via Bash."""
        event = progress_tracker.record_tool_call(
            session_id=session_id,
            tool_name="Bash",
            tool_args={"command": 'git commit -m "Add feature"'},
            tool_result="[main abc1234] Add feature",
        )

        assert event is not None
        assert event.progress_type == ProgressType.COMMIT_CREATED
        assert event.is_high_value is True

    def test_record_tool_call_includes_details(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test that tool call records include details."""
        event = progress_tracker.record_tool_call(
            session_id=session_id,
            tool_name="Edit",
            tool_args={"file_path": "/test.py", "old_string": "foo"},
            tool_result="File edited successfully",
        )

        assert "tool_args_keys" in event.details
        assert "result_type" in event.details
        assert event.details["result_type"] == "str"

    def test_record_tool_call_for_unknown_tool(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test recording unknown tool defaults to TOOL_CALL."""
        event = progress_tracker.record_tool_call(
            session_id=session_id,
            tool_name="UnknownTool",
            tool_args={"arg": "value"},
        )

        assert event is not None
        assert event.progress_type == ProgressType.TOOL_CALL


class TestProgressTrackerSummary:
    """Tests for ProgressTracker.get_summary method."""

    def test_get_summary_empty_session(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test summary for session with no events."""
        summary = progress_tracker.get_summary(session_id)

        assert summary.session_id == session_id
        assert summary.total_events == 0
        assert summary.high_value_events == 0
        assert summary.last_high_value_at is None
        assert summary.last_event_at is None
        assert summary.events_by_type == {}
        assert summary.is_stagnant is False

    def test_get_summary_with_events(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test summary with multiple events."""
        # Record various events
        progress_tracker.record_event(session_id, ProgressType.FILE_READ)
        progress_tracker.record_event(session_id, ProgressType.FILE_READ)
        progress_tracker.record_event(session_id, ProgressType.FILE_MODIFIED)
        progress_tracker.record_event(session_id, ProgressType.COMMIT_CREATED)

        summary = progress_tracker.get_summary(session_id)

        assert summary.total_events == 4
        assert summary.high_value_events == 2  # FILE_MODIFIED + COMMIT_CREATED
        assert summary.events_by_type[ProgressType.FILE_READ] == 2
        assert summary.events_by_type[ProgressType.FILE_MODIFIED] == 1
        assert summary.events_by_type[ProgressType.COMMIT_CREATED] == 1
        assert summary.last_high_value_at is not None
        assert summary.last_event_at is not None

    def test_get_summary_timestamps(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test that summary timestamps are accurate."""
        # Record low-value first
        progress_tracker.record_event(session_id, ProgressType.FILE_READ)

        # Record high-value
        progress_tracker.record_event(session_id, ProgressType.FILE_MODIFIED)
        progress_tracker.db.execute(
            "UPDATE loop_progress SET recorded_at = ? WHERE session_id = ? AND is_high_value = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), session_id, True),
        )

        # Record another low-value
        progress_tracker.record_event(session_id, ProgressType.FILE_READ)

        summary = progress_tracker.get_summary(session_id)

        # Last event should be FILE_READ
        # Last high-value should be FILE_MODIFIED
        assert summary.last_event_at > summary.last_high_value_at


class TestProgressTrackerStagnation:
    """Tests for ProgressTracker stagnation detection."""

    def test_not_stagnant_with_no_events(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test that session with no events is not stagnant."""
        assert progress_tracker.is_stagnant(session_id) is False

    def test_not_stagnant_with_recent_high_value(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test that session with recent high-value progress is not stagnant."""
        progress_tracker.record_event(session_id, ProgressType.FILE_MODIFIED)

        assert progress_tracker.is_stagnant(session_id) is False

    def test_stagnant_by_event_count(self, test_db: HubDatabase, session_id: str) -> None:
        """Test stagnation detection by low-value event count."""
        # Create tracker with low threshold for testing
        tracker = ProgressTracker(
            test_db,
            stagnation_threshold=3600,  # High time threshold
            max_low_value_events=5,  # Low event threshold
        )

        # Record many low-value events without any high-value
        for _ in range(6):
            tracker.record_event(session_id, ProgressType.FILE_READ)

        summary = tracker.get_summary(session_id)
        assert summary.is_stagnant is True
        assert summary.high_value_events == 0
        assert summary.total_events == 6

    def test_not_stagnant_with_mixed_events(self, test_db: HubDatabase, session_id: str) -> None:
        """Test that high-value events prevent stagnation detection."""
        tracker = ProgressTracker(
            test_db,
            stagnation_threshold=3600,
            max_low_value_events=5,
        )

        # Record low-value events
        for _ in range(10):
            tracker.record_event(session_id, ProgressType.FILE_READ)

        # Add a high-value event
        tracker.record_event(session_id, ProgressType.FILE_MODIFIED)

        # Should not be stagnant because we have high-value events
        assert tracker.is_stagnant(session_id) is False

    def test_stagnant_by_time(self, test_db: HubDatabase, session_id: str) -> None:
        """Test stagnation detection by time threshold."""
        tracker = ProgressTracker(
            test_db,
            stagnation_threshold=0.01,  # Very short threshold for testing
            max_low_value_events=100,  # High event threshold
        )

        # Record a high-value event
        tracker.record_event(session_id, ProgressType.FILE_MODIFIED)
        tracker.db.execute(
            "UPDATE loop_progress SET recorded_at = ? WHERE session_id = ? AND is_high_value = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), session_id, True),
        )

        # Record low-value events
        tracker.record_event(session_id, ProgressType.FILE_READ)

        summary = tracker.get_summary(session_id)
        assert summary.is_stagnant is True
        assert summary.stagnation_duration_seconds >= 0.01


class TestProgressTrackerClearSession:
    """Tests for ProgressTracker.clear_session method."""

    def test_clear_session_removes_events(
        self, progress_tracker: ProgressTracker, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test that clear_session removes all events."""
        # Record some events
        progress_tracker.record_event(session_id, ProgressType.FILE_MODIFIED)
        progress_tracker.record_event(session_id, ProgressType.FILE_READ)

        # Verify events exist
        summary = progress_tracker.get_summary(session_id)
        assert summary.total_events == 2

        # Clear session
        count = progress_tracker.clear_session(session_id)
        assert count == 2

        # Verify events are gone
        summary = progress_tracker.get_summary(session_id)
        assert summary.total_events == 0

    def test_clear_session_returns_zero_for_empty(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test that clear_session returns 0 for session with no events."""
        count = progress_tracker.clear_session(session_id)
        assert count == 0

    def test_clear_session_only_affects_specified_session(
        self,
        progress_tracker: ProgressTracker,
        session_id: str,
        session_manager: SessionManager,
        test_project: dict,
    ) -> None:
        """Test that clear_session only removes events for specified session."""
        other_session = create_session(session_manager, test_project["id"], "ext-other-session-456")

        progress_tracker.record_event(session_id, ProgressType.FILE_MODIFIED)
        progress_tracker.record_event(other_session, ProgressType.FILE_MODIFIED)

        # Clear first session
        progress_tracker.clear_session(session_id)

        # Verify other session still has events
        summary = progress_tracker.get_summary(other_session)
        assert summary.total_events == 1


class TestProgressTrackerRecentEvents:
    """Tests for ProgressTracker.get_recent_events method."""

    def test_get_recent_events(self, progress_tracker: ProgressTracker, session_id: str) -> None:
        """Test getting recent events."""
        progress_tracker.record_event(session_id, ProgressType.FILE_READ)
        progress_tracker.record_event(session_id, ProgressType.FILE_MODIFIED)
        progress_tracker.record_event(session_id, ProgressType.COMMIT_CREATED)

        events = progress_tracker.get_recent_events(session_id, limit=10)

        assert len(events) == 3
        # Most recent first
        assert events[0].progress_type == ProgressType.COMMIT_CREATED
        assert events[1].progress_type == ProgressType.FILE_MODIFIED
        assert events[2].progress_type == ProgressType.FILE_READ

    def test_get_recent_events_respects_limit(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test that limit is respected."""
        for _ in range(10):
            progress_tracker.record_event(session_id, ProgressType.FILE_READ)

        events = progress_tracker.get_recent_events(session_id, limit=5)
        assert len(events) == 5

    def test_get_recent_events_empty_session(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test getting events from empty session."""
        events = progress_tracker.get_recent_events(session_id)
        assert events == []


class TestProgressTrackerThreadSafety:
    """Tests for ProgressTracker thread safety."""

    def test_concurrent_record_events(
        self, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test that concurrent event recording is thread-safe."""
        num_threads = 10
        events_per_thread = 20
        errors = []

        def record_events(thread_id: int):
            try:
                for i in range(events_per_thread):
                    progress_tracker.record_event(
                        session_id=session_id,
                        progress_type=ProgressType.FILE_READ,
                        details={"thread": thread_id, "event": i},
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_events, args=(i,)) for i in range(num_threads)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        summary = progress_tracker.get_summary(session_id)
        assert summary.total_events == num_threads * events_per_thread


# ==============================================================================
# StopSignal Tests
# ==============================================================================


class TestStopSignal:
    """Tests for StopSignal dataclass."""

    def test_create_stop_signal(self, session_id: str) -> None:
        """Test creating a stop signal."""
        now = datetime.now(UTC)
        signal = StopSignal(
            session_id=session_id,
            source="http",
            reason="User requested stop",
            requested_at=now,
        )

        assert signal.session_id == session_id
        assert signal.source == "http"
        assert signal.reason == "User requested stop"
        assert signal.requested_at == now
        assert signal.acknowledged_at is None

    def test_is_pending_when_not_acknowledged(self, session_id: str) -> None:
        """Test is_pending returns True when not acknowledged."""
        signal = StopSignal(
            session_id=session_id,
            source="cli",
            reason=None,
            requested_at=datetime.now(UTC),
            acknowledged_at=None,
        )

        assert signal.is_pending is True

    def test_is_pending_when_acknowledged(self, session_id: str) -> None:
        """Test is_pending returns False when acknowledged."""
        now = datetime.now(UTC)
        signal = StopSignal(
            session_id=session_id,
            source="cli",
            reason=None,
            requested_at=now,
            acknowledged_at=now,
        )

        assert signal.is_pending is False


# ==============================================================================
# StopRegistry Tests
# ==============================================================================


class TestStopRegistry:
    """Tests for StopRegistry class."""

    def test_signal_stop_creates_signal(
        self, stop_registry: StopRegistry, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test that signal_stop creates a new stop signal."""
        signal = stop_registry.signal_stop(
            session_id=session_id,
            source="http",
            reason="Testing stop",
        )

        assert signal.session_id == session_id
        assert signal.source == "http"
        assert signal.reason == "Testing stop"
        assert signal.is_pending is True

        # Verify in database
        row = test_db.fetchone(
            "SELECT * FROM session_stop_signals WHERE session_id = ?",
            (session_id,),
        )
        assert row is not None
        assert row["source"] == "http"

    def test_signal_stop_returns_existing_if_pending(
        self, stop_registry: StopRegistry, session_id: str
    ) -> None:
        """Test that signal_stop returns existing pending signal."""
        # Create first signal
        first_signal = stop_registry.signal_stop(
            session_id=session_id,
            source="http",
            reason="First request",
        )

        # Try to create second signal
        second_signal = stop_registry.signal_stop(
            session_id=session_id,
            source="cli",
            reason="Second request",
        )

        # Should return the first signal
        assert second_signal.source == "http"
        assert second_signal.reason == "First request"
        assert first_signal.requested_at == second_signal.requested_at

    def test_get_signal_returns_signal(self, stop_registry: StopRegistry, session_id: str) -> None:
        """Test get_signal returns the signal."""
        stop_registry.signal_stop(session_id, source="mcp")

        signal = stop_registry.get_signal(session_id)

        assert signal is not None
        assert signal.session_id == session_id
        assert signal.source == "mcp"

    def test_get_signal_returns_none_for_unknown(
        self, stop_registry: StopRegistry, session_id: str
    ) -> None:
        """Test get_signal returns None for unknown session."""
        signal = stop_registry.get_signal("unknown-session")
        assert signal is None

    def test_has_pending_signal(self, stop_registry: StopRegistry, session_id: str) -> None:
        """Test has_pending_signal detection."""
        assert stop_registry.has_pending_signal(session_id) is False

        stop_registry.signal_stop(session_id, source="test")

        assert stop_registry.has_pending_signal(session_id) is True

    def test_acknowledge_signal(self, stop_registry: StopRegistry, session_id: str) -> None:
        """Test acknowledging a stop signal."""
        stop_registry.signal_stop(session_id, source="test")

        result = stop_registry.acknowledge(session_id)

        assert result is True
        assert stop_registry.has_pending_signal(session_id) is False

        signal = stop_registry.get_signal(session_id)
        assert signal is not None
        assert signal.acknowledged_at is not None

    def test_acknowledge_returns_false_for_no_signal(
        self, stop_registry: StopRegistry, session_id: str
    ) -> None:
        """Test acknowledge returns False when no signal exists."""
        result = stop_registry.acknowledge(session_id)
        assert result is False

    def test_acknowledge_is_idempotent(self, stop_registry: StopRegistry, session_id: str) -> None:
        """Test that acknowledging twice doesn't fail."""
        stop_registry.signal_stop(session_id, source="test")

        first_ack = stop_registry.acknowledge(session_id)
        second_ack = stop_registry.acknowledge(session_id)

        assert first_ack is True
        assert second_ack is False  # Already acknowledged

    def test_clear_signal(
        self, stop_registry: StopRegistry, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test clearing a stop signal."""
        stop_registry.signal_stop(session_id, source="test")

        result = stop_registry.clear(session_id)

        assert result is True
        assert stop_registry.get_signal(session_id) is None

    def test_clear_returns_false_for_no_signal(
        self, stop_registry: StopRegistry, session_id: str
    ) -> None:
        """Test clear returns False when no signal exists."""
        result = stop_registry.clear(session_id)
        assert result is False


class TestStopRegistryListPending:
    """Tests for StopRegistry.list_pending method."""

    def test_list_pending_empty(self, stop_registry: StopRegistry) -> None:
        """Test list_pending with no signals."""
        signals = stop_registry.list_pending()
        assert signals == []

    def test_list_pending_returns_only_pending(
        self,
        stop_registry: StopRegistry,
        session_manager: SessionManager,
        test_project: dict,
    ) -> None:
        """Test list_pending only returns unacknowledged signals."""
        session_1 = create_session(session_manager, test_project["id"], "ext-session-1")
        session_2 = create_session(session_manager, test_project["id"], "ext-session-2")
        session_3 = create_session(session_manager, test_project["id"], "ext-session-3")

        stop_registry.signal_stop(session_1, source="test")
        stop_registry.signal_stop(session_2, source="test")
        stop_registry.signal_stop(session_3, source="test")

        # Acknowledge one
        stop_registry.acknowledge(session_2)

        pending = stop_registry.list_pending()

        assert len(pending) == 2
        session_ids = [s.session_id for s in pending]
        assert session_1 in session_ids
        assert session_3 in session_ids
        assert session_2 not in session_ids


class TestStopRegistryCleanup:
    """Tests for StopRegistry.cleanup_stale method."""

    def test_cleanup_stale_removes_old_acknowledged(
        self, stop_registry: StopRegistry, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test that cleanup removes old acknowledged signals."""
        # Create and acknowledge a signal
        stop_registry.signal_stop(session_id, source="test")
        stop_registry.acknowledge(session_id)

        # Manually backdate the acknowledged_at
        test_db.execute(
            """
            UPDATE session_stop_signals
            SET acknowledged_at = ?
            WHERE session_id = ?
            """,
            ((datetime.now(UTC) - timedelta(hours=48)).isoformat(), session_id),
        )

        count = stop_registry.cleanup_stale(max_age_hours=24)

        # Should have cleaned up exactly one signal
        assert count == 1
        # Verify signal is gone
        assert stop_registry.has_pending_signal(session_id) is False

    def test_cleanup_stale_preserves_pending(
        self, stop_registry: StopRegistry, session_id: str
    ) -> None:
        """Test that cleanup preserves pending (unacknowledged) signals."""
        stop_registry.signal_stop(session_id, source="test")

        count = stop_registry.cleanup_stale(max_age_hours=0)

        # Should not clean up pending signals
        assert count == 0, "Pending signals should not be cleaned up"
        assert stop_registry.has_pending_signal(session_id) is True


class TestStopRegistryThreadSafety:
    """Tests for StopRegistry thread safety."""

    def test_concurrent_signal_stop(self, stop_registry: StopRegistry, session_id: str) -> None:
        """Test concurrent signal_stop calls are thread-safe."""
        signals = []
        errors = []

        def signal():
            try:
                signal = stop_registry.signal_stop(session_id, source="thread")
                signals.append(signal)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=signal) for _ in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # All should return the same signal (first one wins)
        assert all(s.source == "thread" for s in signals)


# ==============================================================================
# StuckDetectionResult Tests
# ==============================================================================


class TestStuckDetectionResult:
    """Tests for StuckDetectionResult dataclass."""

    def test_create_not_stuck_result(self) -> None:
        """Test creating a not-stuck result."""
        result = StuckDetectionResult(is_stuck=False)

        assert result.is_stuck is False
        assert result.reason is None
        assert result.layer is None
        assert result.details is None
        assert result.suggested_action is None

    def test_create_stuck_result(self) -> None:
        """Test creating a stuck result with details."""
        result = StuckDetectionResult(
            is_stuck=True,
            reason="Task loop detected",
            layer="task_loop",
            details={"task_id": "gt-123", "count": 5},
            suggested_action="change_approach",
        )

        assert result.is_stuck is True
        assert result.reason == "Task loop detected"
        assert result.layer == "task_loop"
        assert result.details["task_id"] == "gt-123"
        assert result.suggested_action == "change_approach"


# ==============================================================================
# TaskSelectionEvent Tests
# ==============================================================================


class TestTaskSelectionEvent:
    """Tests for TaskSelectionEvent dataclass."""

    def test_create_task_selection_event(self, session_id: str) -> None:
        """Test creating a task selection event."""
        now = datetime.now(UTC)
        event = TaskSelectionEvent(
            session_id=session_id,
            task_id="gt-abc123",
            selected_at=now,
            context={"reason": "highest priority"},
        )

        assert event.session_id == session_id
        assert event.task_id == "gt-abc123"
        assert event.selected_at == now
        assert event.context == {"reason": "highest priority"}


# ==============================================================================
# StuckDetector Tests
# ==============================================================================


class TestStuckDetector:
    """Tests for StuckDetector class."""

    def test_init_with_defaults(self, test_db: HubDatabase) -> None:
        """Test initialization with default thresholds."""
        detector = StuckDetector(test_db)

        assert detector.task_loop_threshold == StuckDetector.DEFAULT_TASK_LOOP_THRESHOLD
        assert detector.task_window_size == StuckDetector.DEFAULT_TASK_WINDOW_SIZE
        assert detector.tool_loop_threshold == StuckDetector.DEFAULT_TOOL_LOOP_THRESHOLD
        assert detector.tool_window_size == StuckDetector.DEFAULT_TOOL_WINDOW_SIZE

    def test_init_with_custom_thresholds(self, test_db: HubDatabase) -> None:
        """Test initialization with custom thresholds."""
        detector = StuckDetector(
            test_db,
            task_loop_threshold=5,
            task_window_size=20,
            tool_loop_threshold=10,
            tool_window_size=30,
        )

        assert detector.task_loop_threshold == 5
        assert detector.task_window_size == 20
        assert detector.tool_loop_threshold == 10
        assert detector.tool_window_size == 30

    def test_record_task_selection(
        self, stuck_detector: StuckDetector, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test recording a task selection."""
        event = stuck_detector.record_task_selection(
            session_id=session_id,
            task_id="gt-abc123",
            context={"method": "suggest_next_task"},
        )

        assert event.session_id == session_id
        assert event.task_id == "gt-abc123"
        assert event.context == {"method": "suggest_next_task"}

        # Verify in database
        row = test_db.fetchone(
            "SELECT * FROM task_selection_history WHERE session_id = ?",
            (session_id,),
        )
        assert row is not None
        assert row["task_id"] == "gt-abc123"


class TestStuckDetectorTaskLoop:
    """Tests for StuckDetector task loop detection."""

    def test_no_task_loop_with_no_history(
        self, stuck_detector: StuckDetector, session_id: str
    ) -> None:
        """Test no task loop detected with no history."""
        result = stuck_detector.detect_task_loop(session_id)

        assert result.is_stuck is False

    def test_no_task_loop_with_varied_tasks(
        self, stuck_detector: StuckDetector, session_id: str
    ) -> None:
        """Test no task loop with varied task selections."""
        for i in range(5):
            stuck_detector.record_task_selection(session_id, f"task-{i}")

        result = stuck_detector.detect_task_loop(session_id)

        assert result.is_stuck is False

    def test_task_loop_detected(self, test_db: HubDatabase, session_id: str) -> None:
        """Test task loop detection when same task selected repeatedly."""
        detector = StuckDetector(test_db, task_loop_threshold=3)

        # Select same task multiple times
        for _ in range(4):
            detector.record_task_selection(session_id, "stuck-task-123")

        result = detector.detect_task_loop(session_id)

        assert result.is_stuck is True
        assert result.layer == "task_loop"
        assert "stuck-task-123" in result.reason
        assert result.suggested_action == "change_approach"

    def test_task_loop_threshold_boundary(self, test_db: HubDatabase, session_id: str) -> None:
        """Test task loop at exact threshold."""
        detector = StuckDetector(test_db, task_loop_threshold=3)

        # Select same task exactly threshold times
        for _ in range(3):
            detector.record_task_selection(session_id, "boundary-task")

        result = detector.detect_task_loop(session_id)

        # Should be stuck at >= threshold
        assert result.is_stuck is True


class TestStuckDetectorProgressStagnation:
    """Tests for StuckDetector progress stagnation detection."""

    def test_no_stagnation_without_progress_tracker(
        self, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test no stagnation detection without progress tracker."""
        detector = StuckDetector(test_db, progress_tracker=None)

        result = detector.detect_progress_stagnation(session_id)

        assert result.is_stuck is False

    def test_no_stagnation_with_recent_progress(
        self, stuck_detector: StuckDetector, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test no stagnation with recent high-value progress."""
        progress_tracker.record_event(session_id, ProgressType.FILE_MODIFIED)

        result = stuck_detector.detect_progress_stagnation(session_id)

        assert result.is_stuck is False

    def test_stagnation_detected(self, test_db: HubDatabase, session_id: str) -> None:
        """Test stagnation detection."""
        tracker = ProgressTracker(
            test_db,
            stagnation_threshold=0.01,  # Very short for testing
            max_low_value_events=100,
        )
        detector = StuckDetector(test_db, progress_tracker=tracker)

        # Record high-value event
        tracker.record_event(session_id, ProgressType.FILE_MODIFIED)
        tracker.db.execute(
            "UPDATE loop_progress SET recorded_at = ? WHERE session_id = ? AND is_high_value = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), session_id, True),
        )

        # Record low-value event to update last_event_at
        tracker.record_event(session_id, ProgressType.FILE_READ)

        result = detector.detect_progress_stagnation(session_id)

        assert result.is_stuck is True
        assert result.layer == "progress_stagnation"
        assert result.suggested_action == "stop"


class TestStuckDetectorToolLoop:
    """Tests for StuckDetector tool loop detection."""

    def test_no_tool_loop_without_progress_tracker(
        self, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test no tool loop detection without progress tracker."""
        detector = StuckDetector(test_db, progress_tracker=None)

        result = detector.detect_tool_loop(session_id)

        assert result.is_stuck is False

    def test_no_tool_loop_with_varied_tools(
        self, stuck_detector: StuckDetector, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test no tool loop with varied tool calls."""
        tools = ["Read", "Edit", "Bash", "Glob", "Grep"]
        for tool in tools:
            progress_tracker.record_tool_call(
                session_id, tool, tool_args={"path": f"/unique/{tool}"}
            )

        result = stuck_detector.detect_tool_loop(session_id)

        assert result.is_stuck is False

    def test_tool_loop_detected(self, test_db: HubDatabase, session_id: str) -> None:
        """Test tool loop detection with repeated identical calls."""
        tracker = ProgressTracker(test_db)
        detector = StuckDetector(
            test_db,
            progress_tracker=tracker,
            tool_loop_threshold=4,
            tool_window_size=10,
        )

        # Make identical tool calls
        for _ in range(5):
            tracker.record_tool_call(
                session_id,
                "Read",
                tool_args={"file_path": "/same/file.py"},
            )

        result = detector.detect_tool_loop(session_id)

        assert result.is_stuck is True
        assert result.layer == "tool_loop"
        assert "Read" in result.reason
        assert result.suggested_action == "change_approach"


class TestStuckDetectorIsStuck:
    """Tests for StuckDetector.is_stuck comprehensive check."""

    def test_is_stuck_returns_not_stuck_when_healthy(
        self, stuck_detector: StuckDetector, progress_tracker: ProgressTracker, session_id: str
    ) -> None:
        """Test is_stuck returns not stuck for healthy session."""
        # Record varied progress
        progress_tracker.record_event(session_id, ProgressType.FILE_MODIFIED)
        progress_tracker.record_event(session_id, ProgressType.FILE_READ)

        # Record varied task selections
        stuck_detector.record_task_selection(session_id, "task-1")
        stuck_detector.record_task_selection(session_id, "task-2")

        result = stuck_detector.is_stuck(session_id)

        assert result.is_stuck is False

    def test_is_stuck_returns_first_detected_issue(
        self, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test is_stuck returns first detected stuck state."""
        tracker = ProgressTracker(test_db)
        detector = StuckDetector(
            test_db,
            progress_tracker=tracker,
            task_loop_threshold=2,
        )

        # Create task loop
        for _ in range(3):
            detector.record_task_selection(session_id, "loop-task")

        result = detector.is_stuck(session_id)

        # Should detect task loop first
        assert result.is_stuck is True
        assert result.layer == "task_loop"


class TestStuckDetectorClearSession:
    """Tests for StuckDetector.clear_session method."""

    def test_clear_session(
        self, stuck_detector: StuckDetector, test_db: HubDatabase, session_id: str
    ) -> None:
        """Test clearing session history."""
        stuck_detector.record_task_selection(session_id, "task-1")
        stuck_detector.record_task_selection(session_id, "task-2")

        count = stuck_detector.clear_session(session_id)

        assert count == 2

        # Verify history is cleared
        history = stuck_detector.get_selection_history(session_id)
        assert len(history) == 0

    def test_clear_session_returns_zero_for_empty(
        self, stuck_detector: StuckDetector, session_id: str
    ) -> None:
        """Test clear_session returns 0 for empty session."""
        count = stuck_detector.clear_session(session_id)
        assert count == 0


class TestStuckDetectorSelectionHistory:
    """Tests for StuckDetector.get_selection_history method."""

    def test_get_selection_history(self, stuck_detector: StuckDetector, session_id: str) -> None:
        """Test getting selection history."""
        stuck_detector.record_task_selection(session_id, "task-1")
        stuck_detector.record_task_selection(session_id, "task-2")
        stuck_detector.record_task_selection(session_id, "task-3")

        history = stuck_detector.get_selection_history(session_id, limit=10)

        assert len(history) == 3
        # Most recent first
        assert history[0].task_id == "task-3"
        assert history[2].task_id == "task-1"

    def test_get_selection_history_respects_limit(
        self, stuck_detector: StuckDetector, session_id: str
    ) -> None:
        """Test history respects limit."""
        for i in range(10):
            stuck_detector.record_task_selection(session_id, f"task-{i}")

        history = stuck_detector.get_selection_history(session_id, limit=5)

        assert len(history) == 5

    def test_get_selection_history_empty(
        self, stuck_detector: StuckDetector, session_id: str
    ) -> None:
        """Test history for empty session."""
        history = stuck_detector.get_selection_history(session_id)
        assert history == []

    def test_get_selection_history_migrates_legacy_python_literal_context(
        self,
        stuck_detector: StuckDetector,
        test_db: HubDatabase,
        session_id: str,
    ) -> None:
        """Legacy Python-literal contexts are migrated before JSON-only parsing."""
        test_db.execute(
            "ALTER TABLE task_selection_history ALTER COLUMN context TYPE TEXT USING context::text"
        )
        test_db.execute(
            """
            INSERT INTO task_selection_history (session_id, task_id, selected_at, context)
            VALUES (?, ?, ?, ?)
            """,
            (
                session_id,
                "task-legacy",
                datetime.now(UTC).isoformat(),
                "{'method': 'suggest_next_task'}",
            ),
        )

        history = stuck_detector.get_selection_history(session_id)
        row = test_db.fetchone(
            "SELECT context FROM task_selection_history WHERE session_id = ?",
            (session_id,),
        )

        assert len(history) == 1
        assert history[0].context == {"method": "suggest_next_task"}
        assert row is not None
        assert row["context"] == '{"method": "suggest_next_task"}'


class TestStuckDetectorThreadSafety:
    """Tests for StuckDetector thread safety."""

    def test_concurrent_task_selections(
        self, stuck_detector: StuckDetector, session_id: str
    ) -> None:
        """Test concurrent task selection recording is thread-safe."""
        num_threads = 5
        selections_per_thread = 10
        errors = []

        def record_selections(thread_id: int):
            try:
                for i in range(selections_per_thread):
                    stuck_detector.record_task_selection(
                        session_id=session_id,
                        task_id=f"task-{thread_id}-{i}",
                        context={"thread": thread_id},
                    )
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=record_selections, args=(i,)) for i in range(num_threads)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        history = stuck_detector.get_selection_history(
            session_id, limit=num_threads * selections_per_thread
        )
        assert len(history) == num_threads * selections_per_thread


# ==============================================================================
# Integration Tests
# ==============================================================================


class TestAutonomousIntegration:
    """Integration tests combining multiple autonomous modules."""

    def test_full_autonomous_workflow(
        self,
        test_db: HubDatabase,
        progress_tracker: ProgressTracker,
        stop_registry: StopRegistry,
        stuck_detector: StuckDetector,
        session_id: str,
    ) -> None:
        """Test a complete autonomous workflow scenario."""
        # Session starts work
        progress_tracker.record_event(session_id, ProgressType.TASK_STARTED)
        stuck_detector.record_task_selection(session_id, "gt-task-1")

        # Session makes progress
        progress_tracker.record_tool_call(session_id, "Read", {"file_path": "/src/main.py"})
        progress_tracker.record_tool_call(
            session_id, "Edit", {"file_path": "/src/main.py", "old_string": "a"}
        )

        # Check not stuck
        result = stuck_detector.is_stuck(session_id)
        assert result.is_stuck is False

        # Verify no stop signal
        assert stop_registry.has_pending_signal(session_id) is False

        # User requests stop
        stop_registry.signal_stop(session_id, source="http", reason="User requested")

        # Session checks for stop signal
        assert stop_registry.has_pending_signal(session_id) is True

        # Session acknowledges and stops
        stop_registry.acknowledge(session_id)

        # Record completion
        progress_tracker.record_event(session_id, ProgressType.TASK_COMPLETED)

        # Get final summary
        summary = progress_tracker.get_summary(session_id)
        assert summary.high_value_events >= 2  # Edit + Task completed

    def test_stuck_detection_leads_to_stop(
        self,
        test_db: HubDatabase,
        session_id: str,
    ) -> None:
        """Test that stuck detection can trigger a stop signal."""
        tracker = ProgressTracker(test_db)
        registry = StopRegistry(test_db)
        detector = StuckDetector(
            test_db,
            progress_tracker=tracker,
            task_loop_threshold=2,
        )

        # Session gets stuck in task loop
        for _ in range(3):
            detector.record_task_selection(session_id, "problematic-task")
            tracker.record_tool_call(session_id, "Read", {"file_path": "/same/file"})

        # Detect stuck state
        result = detector.is_stuck(session_id)
        assert result.is_stuck is True
        assert result.layer == "task_loop"

        # Workflow signals stop based on stuck detection
        if result.is_stuck:
            registry.signal_stop(
                session_id,
                source="workflow",
                reason=result.reason,
            )

        # Verify stop signal exists
        assert registry.has_pending_signal(session_id) is True
        signal = registry.get_signal(session_id)
        assert signal.source == "workflow"
        assert "problematic-task" in signal.reason

    def test_session_cleanup_on_completion(
        self,
        progress_tracker: ProgressTracker,
        stop_registry: StopRegistry,
        stuck_detector: StuckDetector,
        session_id: str,
    ) -> None:
        """Test cleanup of all autonomous data when session completes."""
        # Create data in all modules
        progress_tracker.record_event(session_id, ProgressType.FILE_MODIFIED)
        stop_registry.signal_stop(session_id, source="test")
        stop_registry.acknowledge(session_id)
        stuck_detector.record_task_selection(session_id, "task-1")

        # Clean up all data
        progress_tracker.clear_session(session_id)
        stop_registry.clear(session_id)
        stuck_detector.clear_session(session_id)

        # Verify all data is cleared
        summary = progress_tracker.get_summary(session_id)
        assert summary.total_events == 0

        assert stop_registry.get_signal(session_id) is None

        history = stuck_detector.get_selection_history(session_id)
        assert len(history) == 0
