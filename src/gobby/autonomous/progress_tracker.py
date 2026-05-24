"""Progress tracking for autonomous session management.

Provides progress tracking for autonomous workflows to detect stagnation
and enable informed decisions about when to stop or redirect work.
"""

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from gobby.hooks.normalization import canonicalize_shell_tool_name, is_shell_tool

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


class ProgressType(str, Enum):
    """Types of progress events."""

    TOOL_CALL = "tool_call"  # Any tool was called
    FILE_MODIFIED = "file_modified"  # A file was modified (Edit, Write)
    FILE_READ = "file_read"  # A file was read
    TASK_STARTED = "task_started"  # A task was set to in_progress
    TASK_COMPLETED = "task_completed"  # A task was closed
    TEST_PASSED = "test_passed"  # Tests passed
    TEST_FAILED = "test_failed"  # Tests failed
    BUILD_SUCCEEDED = "build_succeeded"  # Build succeeded
    BUILD_FAILED = "build_failed"  # Build failed
    COMMIT_CREATED = "commit_created"  # Git commit was created
    ERROR_OCCURRED = "error_occurred"  # An error occurred


# Tool names that indicate meaningful progress
MEANINGFUL_TOOLS = {
    "Edit": ProgressType.FILE_MODIFIED,
    "Write": ProgressType.FILE_MODIFIED,
    "NotebookEdit": ProgressType.FILE_MODIFIED,
    "Bash": ProgressType.TOOL_CALL,  # Could be build/test
    "Read": ProgressType.FILE_READ,
    "Glob": ProgressType.FILE_READ,
    "Grep": ProgressType.FILE_READ,
}

# High-value progress types that reset stagnation
HIGH_VALUE_PROGRESS = {
    ProgressType.FILE_MODIFIED,
    ProgressType.TASK_COMPLETED,
    ProgressType.COMMIT_CREATED,
    ProgressType.TEST_PASSED,
    ProgressType.BUILD_SUCCEEDED,
}


@dataclass
class ProgressEvent:
    """A single progress event."""

    session_id: str
    progress_type: ProgressType
    timestamp: datetime
    tool_name: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_high_value(self) -> bool:
        """Return True if this is a high-value progress event."""
        return self.progress_type in HIGH_VALUE_PROGRESS


@dataclass
class ProgressSummary:
    """Summary of progress for a session."""

    session_id: str
    total_events: int
    high_value_events: int
    last_high_value_at: datetime | None
    last_event_at: datetime | None
    events_by_type: dict[ProgressType, int]
    is_stagnant: bool = False
    stagnation_duration_seconds: float = 0.0


class ProgressTracker:
    """Track progress for autonomous sessions.

    The ProgressTracker records tool calls and other events during
    autonomous execution, enabling detection of stagnation (when the
    session is no longer making meaningful progress).

    Stagnation is detected when:
    1. No high-value progress events for a configured duration
    2. Too many low-value events without high-value events
    3. Repeated identical tool calls (loop detection)
    """

    # Default stagnation threshold in seconds (10 minutes)
    DEFAULT_STAGNATION_THRESHOLD = 600.0

    # Max low-value events before considering stagnant
    DEFAULT_MAX_LOW_VALUE_EVENTS = 50

    def __init__(
        self,
        db: "HubDatabase",
        stagnation_threshold: float | None = None,
        max_low_value_events: int | None = None,
    ):
        """Initialize the progress tracker.

        Args:
            db: Database connection for persistent storage
            stagnation_threshold: Seconds without high-value progress before stagnant
            max_low_value_events: Max low-value events before stagnant
        """
        self.db = db
        self._lock = threading.Lock()
        self.stagnation_threshold = stagnation_threshold or self.DEFAULT_STAGNATION_THRESHOLD
        self.max_low_value_events = max_low_value_events or self.DEFAULT_MAX_LOW_VALUE_EVENTS

    def record_event(
        self,
        session_id: str,
        progress_type: ProgressType,
        tool_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ProgressEvent:
        """Record a progress event.

        Args:
            session_id: The session to record progress for
            progress_type: Type of progress event
            tool_name: Name of the tool that generated this event
            details: Additional details about the event

        Returns:
            The created ProgressEvent
        """
        now = datetime.now(UTC)
        event = ProgressEvent(
            session_id=session_id,
            progress_type=progress_type,
            timestamp=now,
            tool_name=tool_name,
            details=details or {},
        )

        with self._lock:
            self.db.execute(
                """
                INSERT INTO loop_progress (
                    session_id, progress_type, tool_name, details, recorded_at, is_high_value
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    progress_type.value,
                    tool_name,
                    json.dumps(details) if details else None,
                    now.isoformat(),
                    event.is_high_value,
                ),
            )

        logger.debug(
            f"Recorded progress for session {session_id}: "
            f"{progress_type.value} (high_value={event.is_high_value})"
        )

        return event

    def record_tool_call(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any] | None = None,
        tool_result: Any = None,
    ) -> ProgressEvent | None:
        """Record a tool call as a progress event.

        Automatically determines the progress type based on the tool name
        and result.

        Args:
            session_id: The session that made the tool call
            tool_name: Name of the tool that was called
            tool_args: Arguments passed to the tool
            tool_result: Result returned by the tool

        Returns:
            ProgressEvent if recorded, None if tool is not tracked
        """
        canonical_tool_name = str(canonicalize_shell_tool_name(tool_name))

        # Determine progress type from tool name
        progress_type = MEANINGFUL_TOOLS.get(canonical_tool_name, ProgressType.TOOL_CALL)

        # Enhance progress type based on result analysis
        if is_shell_tool(canonical_tool_name):
            # Check for test/build commands
            command = (tool_args or {}).get("command", "")
            if any(kw in command for kw in ["pytest", "test", "npm test", "cargo test"]):
                # Check result for pass/fail
                result_str = str(tool_result) if tool_result else ""
                if "FAILED" in result_str or "error" in result_str.lower():
                    progress_type = ProgressType.TEST_FAILED
                elif "passed" in result_str or "OK" in result_str:
                    progress_type = ProgressType.TEST_PASSED
            elif any(kw in command for kw in ["build", "compile", "npm run build", "cargo build"]):
                result_str = str(tool_result) if tool_result else ""
                if "error" in result_str.lower() or "failed" in result_str.lower():
                    progress_type = ProgressType.BUILD_FAILED
                else:
                    progress_type = ProgressType.BUILD_SUCCEEDED
            elif "git commit" in command:
                progress_type = ProgressType.COMMIT_CREATED

        # Don't track Read/Glob/Grep as high-priority events
        # They're useful but don't represent meaningful progress alone
        details = {
            "tool_args_keys": list((tool_args or {}).keys()),
            "result_type": type(tool_result).__name__ if tool_result else None,
        }

        return self.record_event(
            session_id=session_id,
            progress_type=progress_type,
            tool_name=tool_name,
            details=details,
        )

    def get_summary(self, session_id: str) -> ProgressSummary:
        """Get a summary of progress for a session.

        Args:
            session_id: The session to get summary for

        Returns:
            ProgressSummary with aggregated progress data
        """
        # Get total counts by type
        rows = self.db.fetchall(
            """
            SELECT progress_type, COUNT(*) as count
            FROM loop_progress
            WHERE session_id = ?
            GROUP BY progress_type
            """,
            (session_id,),
        )

        events_by_type: dict[ProgressType, int] = {}
        total_events = 0
        for row in rows:
            ptype = ProgressType(row["progress_type"])
            events_by_type[ptype] = row["count"]
            total_events += row["count"]

        # Count high-value events
        high_value_result = self.db.fetchone(
            """
            SELECT COUNT(*) as count
            FROM loop_progress
            WHERE session_id = ? AND is_high_value IS TRUE
            """,
            (session_id,),
        )
        high_value_events = high_value_result["count"] if high_value_result else 0

        # Get last high-value event time
        last_hv_result = self.db.fetchone(
            """
            SELECT recorded_at
            FROM loop_progress
            WHERE session_id = ? AND is_high_value IS TRUE
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            (session_id,),
        )
        last_high_value_at = (
            datetime.fromisoformat(last_hv_result["recorded_at"]) if last_hv_result else None
        )

        # Get last event time
        last_event_result = self.db.fetchone(
            """
            SELECT recorded_at
            FROM loop_progress
            WHERE session_id = ?
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            (session_id,),
        )
        last_event_at = (
            datetime.fromisoformat(last_event_result["recorded_at"]) if last_event_result else None
        )

        # Calculate stagnation
        is_stagnant, stagnation_duration = self._check_stagnation(
            session_id, high_value_events, total_events, last_high_value_at
        )

        return ProgressSummary(
            session_id=session_id,
            total_events=total_events,
            high_value_events=high_value_events,
            last_high_value_at=last_high_value_at,
            last_event_at=last_event_at,
            events_by_type=events_by_type,
            is_stagnant=is_stagnant,
            stagnation_duration_seconds=stagnation_duration,
        )

    def is_stagnant(self, session_id: str) -> bool:
        """Check if a session is in a stagnant state.

        A session is stagnant if:
        1. No high-value progress for longer than stagnation_threshold
        2. Too many low-value events without high-value progress

        Args:
            session_id: The session to check

        Returns:
            True if the session appears stagnant
        """
        summary = self.get_summary(session_id)
        return summary.is_stagnant

    def _check_stagnation(
        self,
        session_id: str,
        high_value_events: int,
        total_events: int,
        last_high_value_at: datetime | None,
    ) -> tuple[bool, float]:
        """Check for stagnation conditions.

        Args:
            session_id: The session to check
            high_value_events: Count of high-value events
            total_events: Total event count
            last_high_value_at: Timestamp of last high-value event

        Returns:
            Tuple of (is_stagnant, stagnation_duration_seconds)
        """
        now = datetime.now(UTC)

        # No events yet - not stagnant
        if total_events == 0:
            return False, 0.0

        # Calculate time since last high-value event
        if last_high_value_at:
            duration = (now - last_high_value_at).total_seconds()
        else:
            # No high-value events ever - use first event time
            first_event = self.db.fetchone(
                """
                SELECT recorded_at
                FROM loop_progress
                WHERE session_id = ?
                ORDER BY recorded_at ASC
                LIMIT 1
                """,
                (session_id,),
            )
            if first_event:
                first_time = datetime.fromisoformat(first_event["recorded_at"])
                duration = (now - first_time).total_seconds()
            else:
                duration = 0.0

        # Check time-based stagnation
        if duration > self.stagnation_threshold:
            logger.info(
                f"Session {session_id} stagnant: {duration:.0f}s since last high-value event"
            )
            return True, duration

        # Check event count-based stagnation
        low_value_events = total_events - high_value_events
        if high_value_events == 0 and low_value_events >= self.max_low_value_events:
            logger.info(
                f"Session {session_id} stagnant: "
                f"{low_value_events} low-value events without high-value progress"
            )
            return True, duration

        return False, duration

    def clear_session(self, session_id: str) -> int:
        """Clear all progress records for a session.

        Args:
            session_id: The session to clear

        Returns:
            Number of records cleared
        """
        with self._lock:
            result = self.db.execute(
                "DELETE FROM loop_progress WHERE session_id = ?",
                (session_id,),
            )

        if result.rowcount > 0:
            logger.debug(f"Cleared {result.rowcount} progress record(s) for session {session_id}")

        return result.rowcount

    def get_recent_events(self, session_id: str, limit: int = 20) -> list[ProgressEvent]:
        """Get recent progress events for a session.

        Args:
            session_id: The session to get events for
            limit: Maximum number of events to return

        Returns:
            List of recent ProgressEvents
        """
        rows = self.db.fetchall(
            """
            SELECT session_id, progress_type, tool_name, details, recorded_at
            FROM loop_progress
            WHERE session_id = ?
            ORDER BY recorded_at DESC
            LIMIT ?
            """,
            (session_id, limit),
        )

        return [
            ProgressEvent(
                session_id=row["session_id"],
                progress_type=ProgressType(row["progress_type"]),
                timestamp=datetime.fromisoformat(row["recorded_at"]),
                tool_name=row["tool_name"],
                details=json.loads(row["details"]) if row["details"] else {},  # Safe: json loads
            )
            for row in rows
        ]
