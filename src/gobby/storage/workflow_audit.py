"""Workflow audit log storage manager.

Provides persistent storage for workflow decisions (tool permissions,
rule evaluations, step transitions) for explainability and debugging.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gobby.storage.database import DatabaseProtocol, LocalDatabase

logger = logging.getLogger(__name__)


@dataclass
class WorkflowAuditEntry:
    """A single workflow audit log entry."""

    session_id: str
    step: str
    event_type: str  # 'tool_call', 'rule_eval', 'transition', 'exit_check', 'approval'
    result: str  # 'allow', 'block', 'transition', 'skip', 'approved', 'rejected', 'pending'
    reason: str | None = None
    tool_name: str | None = None
    rule_id: str | None = None
    condition: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: int | None = None


class WorkflowAuditManager:
    """Manages workflow audit log entries in SQLite."""

    def __init__(self, db: DatabaseProtocol | None = None):
        """Initialize the audit manager.

        Args:
            db: Optional database instance. If None, creates a new one.
        """
        self._db = db or LocalDatabase()

    @property
    def db(self) -> DatabaseProtocol:
        """Get database instance."""
        return self._db

    def log(
        self,
        session_id: str,
        step: str,
        event_type: str,
        result: str,
        reason: str | None = None,
        tool_name: str | None = None,
        rule_id: str | None = None,
        condition: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> int | None:
        """Log a workflow audit entry.

        Args:
            session_id: The session this entry belongs to
            step: Current workflow step
            event_type: Type of event ('tool_call', 'rule_eval', etc.)
            result: Result of the evaluation ('allow', 'block', etc.)
            reason: Human-readable explanation
            tool_name: Name of tool (for tool_call events)
            rule_id: Rule identifier (for rule_eval events)
            condition: The 'when' clause evaluated
            context: Additional JSON context

        Returns:
            The inserted row ID, or None on failure.
        """
        try:
            timestamp = datetime.now(UTC).isoformat()
            context_json = json.dumps(context) if context else None

            row = self.db.execute(
                """
                INSERT INTO workflow_audit_log
                (session_id, timestamp, step, event_type, tool_name, rule_id, condition, result, reason, context)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    session_id,
                    timestamp,
                    step,
                    event_type,
                    tool_name,
                    rule_id,
                    condition,
                    result,
                    reason,
                    context_json,
                ),
            ).fetchone()
            return int(row["id"]) if row is not None else None
        except Exception as e:
            logger.error(f"Failed to log audit entry: {e}")
            return None

    def log_tool_call(
        self,
        session_id: str,
        step: str,
        tool_name: str,
        result: str,
        reason: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> int | None:
        """Log a tool call permission check.

        Args:
            session_id: Session ID
            step: Current step
            tool_name: Name of the tool
            result: 'allow' or 'block'
            reason: Why the tool was allowed/blocked
            context: Additional context (tool args, etc.)

        Returns:
            Row ID or None.
        """
        return self.log(
            session_id=session_id,
            step=step,
            event_type="tool_call",
            result=result,
            reason=reason,
            tool_name=tool_name,
            context=context,
        )

    def log_rule_eval(
        self,
        session_id: str,
        step: str,
        rule_id: str,
        condition: str,
        result: str,
        reason: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> int | None:
        """Log a rule evaluation.

        Args:
            session_id: Session ID
            step: Current step
            rule_id: Identifier for the rule
            condition: The 'when' clause
            result: 'allow', 'block', 'skip'
            reason: Why the rule fired/didn't fire
            context: Additional context

        Returns:
            Row ID or None.
        """
        return self.log(
            session_id=session_id,
            step=step,
            event_type="rule_eval",
            result=result,
            reason=reason,
            rule_id=rule_id,
            condition=condition,
            context=context,
        )

    def log_transition(
        self,
        session_id: str,
        from_step: str,
        to_step: str,
        reason: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> int | None:
        """Log a step transition.

        Args:
            session_id: Session ID
            from_step: Step transitioning from
            to_step: Step transitioning to
            reason: Why the transition occurred
            context: Additional context (trigger condition, etc.)

        Returns:
            Row ID or None.
        """
        ctx = context or {}
        ctx["from_step"] = from_step
        ctx["to_step"] = to_step
        return self.log(
            session_id=session_id,
            step=from_step,
            event_type="transition",
            result="transition",
            reason=reason or f"Transitioned to '{to_step}'",
            context=ctx,
        )

    def log_exit_check(
        self,
        session_id: str,
        step: str,
        condition: str,
        result: str,
        reason: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> int | None:
        """Log an exit condition check.

        Args:
            session_id: Session ID
            step: Current step
            condition: The exit condition being checked
            result: 'met' or 'unmet'
            reason: Why the condition was met/unmet
            context: Additional context

        Returns:
            Row ID or None.
        """
        return self.log(
            session_id=session_id,
            step=step,
            event_type="exit_check",
            result=result,
            reason=reason,
            condition=condition,
            context=context,
        )

    def log_approval(
        self,
        session_id: str,
        step: str,
        result: str,
        condition_id: str | None = None,
        prompt: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> int | None:
        """Log an approval gate event.

        Args:
            session_id: Session ID
            step: Current step
            result: 'approved', 'rejected', 'pending', 'timeout'
            condition_id: The approval condition ID
            prompt: The approval prompt shown
            context: Additional context

        Returns:
            Row ID or None.
        """
        ctx = context or {}
        if condition_id:
            ctx["condition_id"] = condition_id
        if prompt:
            ctx["prompt"] = prompt
        return self.log(
            session_id=session_id,
            step=step,
            event_type="approval",
            result=result,
            reason=prompt,
            context=ctx,
        )

    def get_entries(
        self,
        session_id: str | None = None,
        event_type: str | None = None,
        result: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WorkflowAuditEntry]:
        """Get audit log entries with optional filters.

        Args:
            session_id: Filter by session ID
            event_type: Filter by event type
            result: Filter by result
            limit: Maximum entries to return
            offset: Number of entries to skip

        Returns:
            List of WorkflowAuditEntry objects.
        """
        conditions = []
        params: list[Any] = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if result:
            conditions.append("result = ?")
            params.append(result)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.extend([limit, offset])

        rows = self.db.fetchall(
            f"""
            SELECT id, session_id, timestamp, step, event_type, tool_name,
                   rule_id, condition, result, reason, context
            FROM workflow_audit_log
            WHERE {where_clause}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,  # nosec B608
            tuple(params),
        )

        entries = []
        for row in rows:
            context_data = {}
            if row["context"]:
                try:
                    context_data = json.loads(row["context"])
                except json.JSONDecodeError:
                    pass

            timestamp = (
                datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else datetime.now(UTC)
            )

            entries.append(
                WorkflowAuditEntry(
                    id=row["id"],
                    session_id=row["session_id"],
                    timestamp=timestamp,
                    step=row["step"],
                    event_type=row["event_type"],
                    tool_name=row["tool_name"],
                    rule_id=row["rule_id"],
                    condition=row["condition"],
                    result=row["result"],
                    reason=row["reason"],
                    context=context_data,
                )
            )

        return entries

    def cleanup_old_entries(self, days: int = 7) -> int:
        """Delete audit entries older than the specified number of days.

        Args:
            days: Number of days to retain entries (default: 7)

        Returns:
            Number of entries deleted.
        """
        try:
            cursor = self.db.execute(
                """
                DELETE FROM workflow_audit_log
                WHERE datetime(timestamp) < datetime('now', ? || ' days')
                """,
                (f"-{days}",),
            )
            return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to cleanup audit entries: {e}")
            return 0

    def get_entry_count(self, session_id: str | None = None) -> int:
        """Get the total number of audit entries.

        Args:
            session_id: Optional session ID filter

        Returns:
            Total count of entries.
        """
        if session_id:
            row = self.db.fetchone(
                "SELECT COUNT(*) as count FROM workflow_audit_log WHERE session_id = ?",
                (session_id,),
            )
        else:
            row = self.db.fetchone("SELECT COUNT(*) as count FROM workflow_audit_log")

        return row["count"] if row else 0
