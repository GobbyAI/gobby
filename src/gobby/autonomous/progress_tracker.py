"""Progress tracking for autonomous session management.

Provides progress tracking for autonomous workflows to detect stagnation
and enable informed decisions about when to stop or redirect work.
"""

import hashlib
import json
import logging
import re
import shlex
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from gobby.hooks.normalization import canonicalize_shell_tool_name, is_shell_tool
from gobby.utils.datetime import parse_stored_datetime, require_stored_datetime

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


class ProgressType(str, Enum):
    """Types of progress events."""

    TOOL_STARTED = "tool_started"  # A tool call is currently in flight
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
    MCP_MUTATION = "mcp_mutation"  # A state-mutating MCP tool call succeeded
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
    ProgressType.MCP_MUTATION,
    ProgressType.TEST_PASSED,
    ProgressType.BUILD_SUCCEEDED,
}

# Inner MCP tool-name prefixes that only read state; everything else mutates.
# Agents doing pure MCP work (wiki ingest/compile, task filing) must reset the
# stagnation clock, or the stuck detector kills healthy research runs.
MCP_READONLY_TOOL_PREFIXES = (
    "list",
    "get",
    "search",
    "read",
    "wait",
    "can_",
    "check",
    "describe",
    "evaluate",
    "fetch",
    "peek",
    "preview",
    "query",
    "recommend",
    "resolve",
    "show",
    "status",
)
MCP_MUTATING_TOOL_PREFIX_DENYLIST = (
    "check_and_fix",
    "status_update",
)


def _normalize_tool_args(tool_args: dict[str, Any] | None) -> str:
    """Return a stable representation of tool arguments for loop detection."""
    return json.dumps(tool_args or {}, sort_keys=True, default=str, separators=(",", ":"))


def _hash_tool_args(tool_args: dict[str, Any] | None) -> str:
    normalized_args = _normalize_tool_args(tool_args)
    return hashlib.sha256(normalized_args.encode("utf-8")).hexdigest()[:16]


def _split_shell_command(command: Any) -> list[str]:
    command_str = str(command or "")
    try:
        return shlex.split(command_str)
    except ValueError:
        return command_str.split()


def _has_adjacent_tokens(tokens: list[str], first: str, second: str) -> bool:
    return any(
        left == first and right == second for left, right in zip(tokens, tokens[1:], strict=False)
    )


def _is_test_command(tokens: list[str]) -> bool:
    return (
        "pytest" in tokens
        or _has_adjacent_tokens(tokens, "npm", "test")
        or _has_adjacent_tokens(tokens, "cargo", "test")
    )


def _is_build_command(tokens: list[str]) -> bool:
    return (
        "build" in tokens or "compile" in tokens or _has_adjacent_tokens(tokens, "cargo", "build")
    )


def _is_git_commit_command(tokens: list[str]) -> bool:
    return _has_adjacent_tokens(tokens, "git", "commit")


def _result_indicates_failure(result_str: str) -> bool:
    result_lower = result_str.lower()
    return any(
        marker in result_lower
        for marker in ("failed", "failure", "error", "not successful", "unsuccessful")
    )


def _result_indicates_test_success(result_str: str) -> bool:
    return "passed" in result_str.lower() or "OK" in result_str


def _is_mcp_proxy_call(tool_name: str) -> bool:
    """Return True for the MCP proxy's call_tool step (any client prefix)."""
    return tool_name == "call_tool" or tool_name.endswith("__call_tool")


_PASSIVE_WAIT_TOOL_LEAVES = (
    "wait_for_output",
    "wait_for_agent",
    "wait_agent",
    "wait",
)
_PASSIVE_WAIT_TOOL_NAMESPACES = ("collaboration", "functions")


def _normalize_tool_identity(tool_name: str) -> str:
    """Normalize client namespaces and separators without losing semantic leaves."""
    return re.sub(r"[^a-z0-9]+", "_", tool_name.casefold()).strip("_")


def _is_passive_wait_tool(tool_name: str) -> bool:
    normalized = _normalize_tool_identity(tool_name)
    for leaf in _PASSIVE_WAIT_TOOL_LEAVES:
        if normalized == leaf:
            return True
        for namespace in _PASSIVE_WAIT_TOOL_NAMESPACES:
            if re.search(rf"(?:^|_){namespace}_?{re.escape(leaf)}$", normalized):
                return True
    return False


def _tool_activity_details(
    canonical_tool_name: str,
    tool_args: dict[str, Any] | None,
) -> tuple[str, bool]:
    """Resolve the semantic tool beneath an MCP proxy wrapper."""
    effective_tool_name = canonical_tool_name
    is_passive_wait = _is_passive_wait_tool(canonical_tool_name)
    if _is_mcp_proxy_call(canonical_tool_name):
        inner_tool_name = str((tool_args or {}).get("tool_name") or "")
        if inner_tool_name:
            effective_tool_name = inner_tool_name
            is_passive_wait = is_passive_wait or _is_passive_wait_tool(inner_tool_name)
    return effective_tool_name, is_passive_wait


def _mcp_result_indicates_failure(result_str: str) -> bool:
    """Detect failed proxied MCP calls from structured payloads when possible."""
    if not result_str:
        return False
    try:
        payload = json.loads(result_str)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _result_indicates_failure(result_str)
    if not isinstance(payload, dict):
        return False
    success = payload.get("success")
    if success is False:
        return True
    if success is True:
        return False
    return bool(payload.get("error") or payload.get("errors"))


def _tool_matches_prefix(tool_name: str, prefix: str) -> bool:
    separator = "" if prefix.endswith("_") else "_"
    return tool_name == prefix or tool_name.startswith(f"{prefix}{separator}")


def _is_readonly_mcp_tool(tool_name: str) -> bool:
    if any(_tool_matches_prefix(tool_name, prefix) for prefix in MCP_MUTATING_TOOL_PREFIX_DENYLIST):
        return False
    return any(_tool_matches_prefix(tool_name, prefix) for prefix in MCP_READONLY_TOOL_PREFIXES)


def _classify_mcp_call(tool_args: dict[str, Any] | None, tool_result: Any) -> ProgressType:
    """Classify a proxied MCP call as mutation progress or a plain tool call.

    Rule blocks deny at before_tool, so any call observed here actually
    executed; only an explicit success=false payload demotes it.
    """
    inner_tool = str((tool_args or {}).get("tool_name") or "")
    if not inner_tool or _is_readonly_mcp_tool(inner_tool):
        return ProgressType.TOOL_CALL
    result_str = str(tool_result) if tool_result else ""
    if _mcp_result_indicates_failure(result_str):
        return ProgressType.TOOL_CALL
    return ProgressType.MCP_MUTATION


def _result_indicates_build_success(result_str: str) -> bool:
    result_lower = result_str.lower()
    negative_success_markers = ("not successful", "not successfully", "unsuccessful")
    if any(marker in result_lower for marker in negative_success_markers):
        return False

    return any(
        marker in result_lower
        for marker in ("completed successfully", "built successfully", "succeeded", "successfully")
    )


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

    Stagnation means the session has gone quiet: no progress events of any
    kind for a configured duration. Event value is deliberately not judged
    here — read-heavy sessions (review, research) emit only low-value events
    yet are making real progress. Busy-loops of identical calls are caught by
    StuckDetector's tool-loop layer, not by this tracker.
    """

    # Default stagnation threshold in seconds (10 minutes)
    DEFAULT_STAGNATION_THRESHOLD = 600.0

    def __init__(
        self,
        db: "HubDatabase",
        stagnation_threshold: float | None = None,
    ):
        """Initialize the progress tracker.

        Args:
            db: Database connection for persistent storage
            stagnation_threshold: Seconds without any progress event before stagnant
        """
        self.db = db
        self._lock = threading.Lock()
        self._consecutive_passive_waits: dict[str, int] = {}
        self.stagnation_threshold = stagnation_threshold or self.DEFAULT_STAGNATION_THRESHOLD

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
        event_details = dict(details or {})

        with self._lock:
            is_passive_wait = event_details.get("is_passive_wait") is True
            if is_passive_wait:
                count = self._consecutive_passive_waits.get(session_id, 0)
                if progress_type is not ProgressType.TOOL_STARTED:
                    count += 1
                event_details["consecutive_passive_waits"] = count
            event = ProgressEvent(
                session_id=session_id,
                progress_type=progress_type,
                timestamp=now,
                tool_name=tool_name,
                details=event_details,
            )
            self.db.execute(
                """
                INSERT INTO loop_progress (
                    session_id, progress_type, tool_name, details, recorded_at, is_high_value
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    progress_type.value,
                    tool_name,
                    json.dumps(event_details) if event_details else None,
                    now.isoformat(),
                    event.is_high_value,
                ),
            )
            if is_passive_wait and progress_type is not ProgressType.TOOL_STARTED:
                self._consecutive_passive_waits[session_id] = count
            elif not is_passive_wait:
                self._consecutive_passive_waits.pop(session_id, None)

        logger.debug(
            "Recorded progress for session %s: %s (high_value=%s)",
            session_id,
            progress_type.value,
            event.is_high_value,
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
        effective_tool_name, is_passive_wait = _tool_activity_details(
            canonical_tool_name,
            tool_args,
        )

        # Determine progress type from tool name
        progress_type = MEANINGFUL_TOOLS.get(canonical_tool_name, ProgressType.TOOL_CALL)

        # Enhance progress type based on result analysis
        if is_shell_tool(canonical_tool_name):
            # Check for test/build commands
            command = (tool_args or {}).get("command", "")
            command_tokens = _split_shell_command(command)
            result_str = str(tool_result) if tool_result else ""
            if _is_git_commit_command(command_tokens):
                progress_type = ProgressType.COMMIT_CREATED
            elif _is_test_command(command_tokens):
                # Check result for pass/fail
                if _result_indicates_failure(result_str):
                    progress_type = ProgressType.TEST_FAILED
                elif _result_indicates_test_success(result_str):
                    progress_type = ProgressType.TEST_PASSED
            elif _is_build_command(command_tokens):
                if _result_indicates_failure(result_str):
                    progress_type = ProgressType.BUILD_FAILED
                elif _result_indicates_build_success(result_str):
                    progress_type = ProgressType.BUILD_SUCCEEDED
        elif _is_mcp_proxy_call(canonical_tool_name):
            progress_type = _classify_mcp_call(tool_args, tool_result)

        # Don't track Read/Glob/Grep as high-priority events
        # They're useful but don't represent meaningful progress alone
        details = {
            "tool_args_keys": list((tool_args or {}).keys()),
            "tool_args_fingerprint": _hash_tool_args(tool_args),
            "result_type": type(tool_result).__name__ if tool_result else None,
            "effective_tool_name": effective_tool_name,
            "is_passive_wait": is_passive_wait,
        }

        return self.record_event(
            session_id=session_id,
            progress_type=progress_type,
            tool_name=tool_name,
            details=details,
        )

    def record_tool_start(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any] | None = None,
    ) -> ProgressEvent:
        """Record that a tool call started and remains in flight."""
        canonical_tool_name = str(canonicalize_shell_tool_name(tool_name))
        effective_tool_name, is_passive_wait = _tool_activity_details(
            canonical_tool_name,
            tool_args,
        )
        return self.record_event(
            session_id=session_id,
            progress_type=ProgressType.TOOL_STARTED,
            tool_name=tool_name,
            details={
                "tool_args_keys": list((tool_args or {}).keys()),
                "tool_args_fingerprint": _hash_tool_args(tool_args),
                "effective_tool_name": effective_tool_name,
                "is_passive_wait": is_passive_wait,
            },
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
            WHERE session_id = %s
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
            WHERE session_id = %s AND is_high_value IS TRUE
            """,
            (session_id,),
        )
        high_value_events = high_value_result["count"] if high_value_result else 0

        # Get last high-value event time
        last_hv_result = self.db.fetchone(
            """
            SELECT recorded_at
            FROM loop_progress
            WHERE session_id = %s AND is_high_value IS TRUE
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1
            """,
            (session_id,),
        )
        last_high_value_at = (
            parse_stored_datetime(last_hv_result["recorded_at"]) if last_hv_result else None
        )

        # Get last event time
        last_event_result = self.db.fetchone(
            """
            SELECT progress_type, recorded_at, details
            FROM loop_progress
            WHERE session_id = %s
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1
            """,
            (session_id,),
        )
        last_event_at = (
            parse_stored_datetime(last_event_result["recorded_at"]) if last_event_result else None
        )
        last_event_type = (
            ProgressType(last_event_result["progress_type"]) if last_event_result else None
        )
        raw_last_details = last_event_result["details"] if last_event_result else None
        if isinstance(raw_last_details, str):
            parsed_last_details = json.loads(raw_last_details)
        elif isinstance(raw_last_details, dict):
            parsed_last_details = raw_last_details
        else:
            parsed_last_details = {}
        last_event_is_passive_wait = parsed_last_details.get("is_passive_wait") is True

        # Calculate stagnation
        is_stagnant, stagnation_duration = self._check_stagnation(
            session_id,
            total_events,
            last_event_at,
            last_event_type,
            last_event_is_passive_wait,
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

        A session is stagnant if it has recorded no progress events of any
        kind for longer than stagnation_threshold.

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
        total_events: int,
        last_event_at: datetime | None,
        last_event_type: ProgressType | None,
        last_event_is_passive_wait: bool,
    ) -> tuple[bool, float]:
        """Check whether the session has gone quiet.

        Duration is measured from the most recent event of any kind: a
        session with actively flowing events is never stagnant, no matter
        how many of them are low-value (#17779).

        Args:
            session_id: The session to check
            total_events: Total event count
            last_event_at: Timestamp of the most recent event
            last_event_type: Type of the most recent event

        Returns:
            Tuple of (is_stagnant, seconds_since_last_event)
        """
        # No events yet - not stagnant
        if total_events == 0 or last_event_at is None:
            return False, 0.0

        duration = (datetime.now(UTC) - last_event_at).total_seconds()
        if last_event_type is ProgressType.TOOL_STARTED and not last_event_is_passive_wait:
            return False, duration

        if duration > self.stagnation_threshold:
            logger.info(
                "Session %s stagnant: %.0fs since last progress event", session_id, duration
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
            self._consecutive_passive_waits.pop(session_id, None)
            result = self.db.execute(
                "DELETE FROM loop_progress WHERE session_id = %s",
                (session_id,),
            )

        if result.rowcount > 0:
            logger.debug(
                "Cleared %s progress record(s) for session %s", result.rowcount, session_id
            )

        return result.rowcount

    def prune_older_than(self, *, retention_days: int) -> int:
        """Delete progress records older than the retention period."""
        with self._lock:
            result = self.db.execute(
                """
                DELETE FROM loop_progress
                WHERE recorded_at < NOW() - (%s * INTERVAL '1 day')
                """,
                (retention_days,),
            )

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
            WHERE session_id = %s
            ORDER BY recorded_at DESC
            LIMIT %s
            """,
            (session_id, limit),
        )

        return [
            ProgressEvent(
                session_id=row["session_id"],
                progress_type=ProgressType(row["progress_type"]),
                timestamp=require_stored_datetime(row["recorded_at"], "recorded_at"),
                tool_name=row["tool_name"],
                details=json.loads(row["details"]) if row["details"] else {},  # Safe: json loads
            )
            for row in rows
        ]
