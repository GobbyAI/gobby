"""Stuck detection for autonomous session management.

Provides multi-layer stuck detection for autonomous workflows:
1. Task selection loop detection - same tasks being selected repeatedly
2. Progress stagnation - no meaningful progress being made
3. Tool call patterns - repeated identical tool calls
"""

import ast
import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.autonomous.progress_tracker import ProgressTracker
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


@dataclass
class TaskSelectionEvent:
    """A task selection event for loop detection."""

    session_id: str
    task_id: str
    selected_at: datetime
    context: dict[str, Any] | None = None


def migrate_task_selection_history_contexts(db: "HubDatabase") -> int:
    """Migrate legacy Python-literal selection contexts to JSON strings."""
    migrated = 0
    rows = db.fetchall(
        """
        SELECT id, context
          FROM task_selection_history
         WHERE context IS NOT NULL
        """
    )
    for row in rows:
        context = row["context"]
        if not isinstance(context, str):
            continue
        try:
            json.loads(context)
            continue
        except json.JSONDecodeError:
            pass
        try:
            parsed = ast.literal_eval(context)
            context_json = json.dumps(parsed)
        except (SyntaxError, ValueError, TypeError):
            continue
        db.execute(
            "UPDATE task_selection_history SET context = ? WHERE id = ?",
            (context_json, row["id"]),
        )
        migrated += 1
    return migrated


def _decode_task_selection_context(raw_context: Any) -> dict[str, Any] | None:
    if not raw_context:
        return None
    if isinstance(raw_context, dict):
        return raw_context
    if not isinstance(raw_context, str):
        return None
    try:
        decoded = json.loads(raw_context)
    except json.JSONDecodeError:
        logger.warning(
            "Failed to parse context for task selection: %s",
            raw_context[:100],
            exc_info=True,
        )
        return None
    return decoded if isinstance(decoded, dict) else None


@dataclass
class StuckDetectionResult:
    """Result of stuck detection analysis."""

    is_stuck: bool
    reason: str | None = None
    layer: str | None = None  # task_loop, progress_stagnation, tool_loop
    details: dict[str, Any] | None = None
    suggested_action: str | None = None  # stop, change_approach, escalate


class StuckDetector:
    """Multi-layer stuck detection for autonomous sessions.

    The stuck detector analyzes session behavior at three levels:

    Layer 1 - Task Selection Loops:
        Detects when the same task(s) are being selected repeatedly
        without successful completion. This indicates the agent is
        unable to make progress on available work.

    Layer 2 - Progress Stagnation:
        Uses ProgressTracker to detect when no meaningful progress
        (file modifications, commits, task completions) is occurring
        despite continued activity.

    Layer 3 - Tool Call Patterns:
        Detects repeated identical tool calls that indicate the agent
        is stuck in a loop (e.g., repeatedly reading the same file).
    """

    # Thresholds for loop detection
    DEFAULT_TASK_LOOP_THRESHOLD = 3  # Same task selected N times = loop
    DEFAULT_TASK_WINDOW_SIZE = 10  # Look at last N selections
    DEFAULT_TOOL_LOOP_THRESHOLD = 5  # Same tool call N times = loop
    DEFAULT_TOOL_WINDOW_SIZE = 20  # Look at last N tool calls

    def __init__(
        self,
        db: "HubDatabase",
        progress_tracker: "ProgressTracker | None" = None,
        task_loop_threshold: int | None = None,
        task_window_size: int | None = None,
        tool_loop_threshold: int | None = None,
        tool_window_size: int | None = None,
    ):
        """Initialize the stuck detector.

        Args:
            db: Database connection for persistent storage
            progress_tracker: Optional ProgressTracker for stagnation detection
            task_loop_threshold: Times a task can be selected before considered stuck
            task_window_size: Number of recent selections to analyze
            tool_loop_threshold: Times same tool call before considered stuck
            tool_window_size: Number of recent tool calls to analyze
        """
        self.db = db
        self.progress_tracker = progress_tracker
        self._lock = threading.Lock()

        self.task_loop_threshold = task_loop_threshold or self.DEFAULT_TASK_LOOP_THRESHOLD
        self.task_window_size = task_window_size or self.DEFAULT_TASK_WINDOW_SIZE
        self.tool_loop_threshold = tool_loop_threshold or self.DEFAULT_TOOL_LOOP_THRESHOLD
        self.tool_window_size = tool_window_size or self.DEFAULT_TOOL_WINDOW_SIZE

    def record_task_selection(
        self,
        session_id: str,
        task_id: str,
        context: dict[str, Any] | None = None,
    ) -> TaskSelectionEvent:
        """Record a task selection event.

        Args:
            session_id: The session selecting the task
            task_id: The task being selected
            context: Optional context about the selection

        Returns:
            The created TaskSelectionEvent
        """
        now = datetime.now(UTC)
        event = TaskSelectionEvent(
            session_id=session_id,
            task_id=task_id,
            selected_at=now,
            context=context,
        )

        with self._lock:
            self.db.execute(
                """
                INSERT INTO task_selection_history (
                    session_id, task_id, selected_at, context
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    session_id,
                    task_id,
                    now.isoformat(),
                    json.dumps(context) if context else None,
                ),
            )

        logger.debug(f"Recorded task selection for session {session_id}: task={task_id}")

        return event

    def detect_task_loop(self, session_id: str) -> StuckDetectionResult:
        """Detect task selection loops.

        Checks the last N task selections (task_window_size) within the past hour
        to detect if any task has been selected more times than the threshold.

        Args:
            session_id: The session to check

        Returns:
            StuckDetectionResult indicating if stuck in task loop
        """
        from datetime import timedelta

        # Compute cutoff as ISO8601 string for like-for-like comparison
        cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

        # Get the last N task selections within the time window, then aggregate
        rows = self.db.fetchall(
            """
            SELECT task_id, COUNT(*) as count
            FROM (
                SELECT task_id
                FROM task_selection_history
                WHERE session_id = ?
                AND selected_at > ?
                ORDER BY selected_at DESC
                LIMIT ?
            )
            GROUP BY task_id
            ORDER BY count DESC
            """,
            (session_id, cutoff, self.task_window_size),
        )

        if not rows:
            return StuckDetectionResult(is_stuck=False)

        # Check if any task has been selected too many times
        for row in rows:
            if row["count"] >= self.task_loop_threshold:
                logger.info(
                    f"Session {session_id} stuck in task loop: "
                    f"task {row['task_id']} selected {row['count']} times"
                )
                return StuckDetectionResult(
                    is_stuck=True,
                    reason=f"Task '{row['task_id']}' selected {row['count']} times without completion",
                    layer="task_loop",
                    details={
                        "task_id": row["task_id"],
                        "selection_count": row["count"],
                        "threshold": self.task_loop_threshold,
                    },
                    suggested_action="change_approach",
                )

        return StuckDetectionResult(is_stuck=False)

    def detect_progress_stagnation(self, session_id: str) -> StuckDetectionResult:
        """Detect progress stagnation using ProgressTracker.

        Args:
            session_id: The session to check

        Returns:
            StuckDetectionResult indicating if progress is stagnant
        """
        if not self.progress_tracker:
            return StuckDetectionResult(is_stuck=False)

        summary = self.progress_tracker.get_summary(session_id)

        if summary.is_stagnant:
            logger.info(
                f"Session {session_id} progress stagnant: "
                f"{summary.stagnation_duration_seconds:.0f}s since high-value event"
            )
            return StuckDetectionResult(
                is_stuck=True,
                reason=f"No meaningful progress for {summary.stagnation_duration_seconds:.0f} seconds",
                layer="progress_stagnation",
                details={
                    "total_events": summary.total_events,
                    "high_value_events": summary.high_value_events,
                    "stagnation_duration": summary.stagnation_duration_seconds,
                    "last_high_value_at": (
                        summary.last_high_value_at.isoformat()
                        if summary.last_high_value_at
                        else None
                    ),
                },
                suggested_action="stop",
            )

        return StuckDetectionResult(is_stuck=False)

    def detect_tool_loop(self, session_id: str) -> StuckDetectionResult:
        """Detect repeated identical tool calls.

        Args:
            session_id: The session to check

        Returns:
            StuckDetectionResult indicating if stuck in tool loop
        """
        # Get recent tool calls from progress tracker
        if not self.progress_tracker:
            return StuckDetectionResult(is_stuck=False)

        recent_events = self.progress_tracker.get_recent_events(session_id, self.tool_window_size)

        if not recent_events:
            return StuckDetectionResult(is_stuck=False)

        # Count tool call patterns
        tool_counts: dict[str, int] = {}
        for event in recent_events:
            if event.tool_name:
                # Create a key from tool name and key args
                key = f"{event.tool_name}:{event.details.get('tool_args_keys', [])}"
                tool_counts[key] = tool_counts.get(key, 0) + 1

        # Check for repeated patterns
        for key, count in tool_counts.items():
            if count >= self.tool_loop_threshold:
                tool_name = key.split(":")[0]
                logger.info(
                    f"Session {session_id} stuck in tool loop: {tool_name} called {count} times"
                )
                return StuckDetectionResult(
                    is_stuck=True,
                    reason=f"Tool '{tool_name}' called {count} times with same pattern",
                    layer="tool_loop",
                    details={
                        "tool_pattern": key,
                        "call_count": count,
                        "threshold": self.tool_loop_threshold,
                    },
                    suggested_action="change_approach",
                )

        return StuckDetectionResult(is_stuck=False)

    def is_stuck(self, session_id: str) -> StuckDetectionResult:
        """Run all stuck detection checks.

        Checks all three layers in order of severity:
        1. Task selection loops
        2. Progress stagnation
        3. Tool call loops

        Args:
            session_id: The session to check

        Returns:
            StuckDetectionResult from first layer that detects stuck state,
            or not-stuck result if all layers pass
        """
        # Layer 1: Task loops
        result = self.detect_task_loop(session_id)
        if result.is_stuck:
            return result

        # Layer 2: Progress stagnation
        result = self.detect_progress_stagnation(session_id)
        if result.is_stuck:
            return result

        # Layer 3: Tool loops
        result = self.detect_tool_loop(session_id)
        if result.is_stuck:
            return result

        return StuckDetectionResult(is_stuck=False)

    def clear_session(self, session_id: str) -> int:
        """Clear all stuck detection data for a session.

        Args:
            session_id: The session to clear

        Returns:
            Number of records cleared
        """
        with self._lock:
            result = self.db.execute(
                "DELETE FROM task_selection_history WHERE session_id = ?",
                (session_id,),
            )

        if result.rowcount > 0:
            logger.debug(
                f"Cleared {result.rowcount} task selection record(s) for session {session_id}"
            )

        return result.rowcount

    def get_selection_history(self, session_id: str, limit: int = 20) -> list[TaskSelectionEvent]:
        """Get recent task selection history.

        Args:
            session_id: The session to get history for
            limit: Maximum number of events to return

        Returns:
            List of recent TaskSelectionEvents
        """
        migrate_task_selection_history_contexts(self.db)
        rows = self.db.fetchall(
            """
            SELECT session_id, task_id, selected_at, context
            FROM task_selection_history
            WHERE session_id = ?
            ORDER BY selected_at DESC
            LIMIT ?
            """,
            (session_id, limit),
        )

        events = []
        for row in rows:
            context = _decode_task_selection_context(row["context"])
            events.append(
                TaskSelectionEvent(
                    session_id=row["session_id"],
                    task_id=row["task_id"],
                    selected_at=datetime.fromisoformat(row["selected_at"]),
                    context=context,
                )
            )
        return events
