"""Cleanup operations for agent run storage."""

from __future__ import annotations

from typing import Protocol

from gobby.storage.database import DatabaseProtocol
from gobby.storage.sql_dialect import elapsed_seconds_greater_than_expr, older_than_now_expr

from ._constants import get_logger
from ._helpers import _positive_rowcount, utc_now_iso
from ._models import AgentRun


class _AgentRunCleanupHost(Protocol):
    db: DatabaseProtocol

    def timeout(
        self,
        run_id: str,
        turns_used: int = 0,
        error: str = "Execution timed out",
        tool_calls_count: int = 0,
    ) -> AgentRun | None: ...


class _AgentRunCleanupMixin:
    def cleanup_stale_runs(self: _AgentRunCleanupHost, default_timeout_minutes: int = 30) -> int:
        """Mark stale running agent runs as timed out and expire their sessions.

        Uses per-agent timeout_seconds when set, falls back to default_timeout_minutes.

        Args:
            default_timeout_minutes: Fallback timeout for runs without timeout_seconds.

        Returns:
            Number of runs timed out.
        """
        explicit_timeout_sql = elapsed_seconds_greater_than_expr(
            self.db,
            "last_activity_at",
            "timeout_seconds",
        )
        default_timeout_sql = older_than_now_expr(self.db, "last_activity_at", "?", "minute")
        stale_runs = self.db.fetchall(
            f"""
            WITH run_activity AS (
                SELECT
                    ar.id,
                    ar.timeout_seconds,
                    COALESCE(child.updated_at, ar.updated_at, ar.started_at) AS last_activity_at,
                    COALESCE(child.tool_call_count, parent.tool_call_count, ar.tool_calls_count, 0)
                        AS tool_calls_count,
                    COALESCE(child.turn_count, parent.turn_count, ar.turns_used, 0) AS turns_used
                FROM agent_runs ar
                LEFT JOIN sessions child ON child.id = ar.child_session_id
                LEFT JOIN sessions parent ON parent.id = ar.parent_session_id
                WHERE ar.status = 'running'
            )
            SELECT
                id,
                timeout_seconds,
                tool_calls_count,
                turns_used
            FROM run_activity
            WHERE (
                timeout_seconds IS NOT NULL
                AND {explicit_timeout_sql}
            )
            OR (
                timeout_seconds IS NULL
                AND {default_timeout_sql}
            )
            """,  # nosec B608 - timeout expressions are selected by storage dialect.
            (default_timeout_minutes,),
        )

        explicit_count = 0
        default_count = 0
        timed_out = 0
        for row in stale_runs:
            timeout_seconds = row["timeout_seconds"]
            error = (
                f"Exceeded timeout ({int(timeout_seconds)}s)"
                if timeout_seconds is not None
                else f"Exceeded default timeout ({default_timeout_minutes}m)"
            )
            updated = self.timeout(
                row["id"],
                turns_used=row["turns_used"] or 0,
                error=error,
                tool_calls_count=row["tool_calls_count"] or 0,
            )
            if updated is None:
                continue
            timed_out += 1
            if timeout_seconds is not None:
                explicit_count += 1
            else:
                default_count += 1

        if timed_out:
            get_logger().info(
                "Timed out %s stale agent runs (%s explicit, %s default)",
                timed_out,
                explicit_count,
                default_count,
            )

        return timed_out

    def cleanup_stale_pending_runs(
        self: _AgentRunCleanupHost,
        timeout_minutes: int = 60,
        long_timeout_minutes: int = 1440,
    ) -> int:
        """Mark stale pending agent runs as failed."""
        now = utc_now_iso()
        pending_timeout_sql = older_than_now_expr(self.db, "created_at", "?", "minute")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE agent_runs
                SET status = 'error',
                    error = CASE
                        WHEN tmux_session_name IS NULL THEN 'Pending run never started'
                        ELSE 'Pending tmux-initialized run never started'
                    END,
                    completed_at = ?,
                    updated_at = ?
                WHERE status = 'pending'
                AND (
                    tmux_session_name IS NULL
                    AND {pending_timeout_sql}
                    OR tmux_session_name IS NOT NULL
                    AND {pending_timeout_sql}
                )
                """,  # nosec B608 - timeout expression is selected by storage dialect.
                (now, now, timeout_minutes, long_timeout_minutes),
            )
        count = _positive_rowcount(cursor)
        if count > 0:
            get_logger().info(
                "Failed %s stale pending agent runs (>%sm; tmux >%sm)",
                count,
                timeout_minutes,
                long_timeout_minutes,
            )
        return count
