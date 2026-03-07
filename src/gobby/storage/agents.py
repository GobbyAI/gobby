"""Storage manager for agent runs."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

AgentRunStatus = Literal["pending", "running", "success", "error", "timeout", "cancelled"]


@dataclass
class AgentRun:
    """Agent run data model."""

    id: str
    parent_session_id: str
    provider: str
    prompt: str
    status: AgentRunStatus
    created_at: str
    updated_at: str
    # Optional fields
    child_session_id: str | None = None
    workflow_name: str | None = None
    model: str | None = None
    result: str | None = None
    error: str | None = None
    tool_calls_count: int = 0
    turns_used: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    sdk_session_id: str | None = None
    continuation_prompt: str | None = None
    task_id: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> AgentRun:
        """Create AgentRun from database row."""
        return cls(
            id=row["id"],
            parent_session_id=row["parent_session_id"],
            child_session_id=row["child_session_id"],
            workflow_name=row["workflow_name"],
            provider=row["provider"],
            model=row["model"],
            status=row["status"],
            prompt=row["prompt"],
            result=row["result"],
            error=row["error"],
            tool_calls_count=row["tool_calls_count"] or 0,
            turns_used=row["turns_used"] or 0,
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            sdk_session_id=row["sdk_session_id"] if "sdk_session_id" in row.keys() else None,
            continuation_prompt=row["continuation_prompt"]
            if "continuation_prompt" in row.keys()
            else None,
            task_id=row["task_id"] if "task_id" in row.keys() else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
            "workflow_name": self.workflow_name,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "prompt": self.prompt,
            "result": self.result,
            "error": self.error,
            "tool_calls_count": self.tool_calls_count,
            "turns_used": self.turns_used,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sdk_session_id": self.sdk_session_id,
            "continuation_prompt": self.continuation_prompt,
            "task_id": self.task_id,
        }


class LocalAgentRunManager:
    """Manager for agent run storage operations."""

    def __init__(self, db: DatabaseProtocol):
        """Initialize with database connection."""
        self.db = db

    def create(
        self,
        parent_session_id: str,
        provider: str,
        prompt: str,
        workflow_name: str | None = None,
        model: str | None = None,
        child_session_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> AgentRun:
        """
        Create a new agent run.

        Args:
            parent_session_id: Session that spawned this agent.
            provider: LLM provider (claude, gemini, etc.)
            prompt: The prompt given to the agent.
            workflow_name: Optional workflow being executed.
            model: Optional model override.
            child_session_id: Optional child session for the agent.
            run_id: Optional pre-generated run ID. If not provided, one is generated.
            task_id: Optional task ID this agent is working on.

        Returns:
            Created AgentRun.
        """
        if run_id is None:
            run_id = f"run-{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC).isoformat()

        self.db.execute(
            """
            INSERT OR REPLACE INTO agent_runs (
                id, parent_session_id, child_session_id, workflow_name,
                provider, model, status, prompt, task_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                run_id,
                parent_session_id,
                child_session_id,
                workflow_name,
                provider,
                model,
                prompt,
                task_id,
                now,
                now,
            ),
        )

        logger.debug(f"Created agent run {run_id} for session {parent_session_id}")
        agent_run = self.get(run_id)
        if agent_run is None:
            raise RuntimeError(f"Failed to retrieve newly created agent run: {run_id}")
        return agent_run

    def get(self, run_id: str) -> AgentRun | None:
        """Get agent run by ID."""
        row = self.db.fetchone("SELECT * FROM agent_runs WHERE id = ?", (run_id,))
        return AgentRun.from_row(row) if row else None

    def has_active_run_for_task(self, task_id: str) -> bool:
        """Check if there's already a pending/running agent run for a task."""
        row = self.db.fetchone(
            "SELECT id FROM agent_runs WHERE task_id = ? AND status IN ('pending', 'running')",
            (task_id,),
        )
        return row is not None

    def start(self, run_id: str) -> AgentRun | None:
        """Mark agent run as started."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'running', started_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, run_id),
        )
        return self.get(run_id)

    def complete(
        self,
        run_id: str,
        result: str,
        tool_calls_count: int = 0,
        turns_used: int = 0,
    ) -> AgentRun | None:
        """
        Mark agent run as completed successfully.

        Args:
            run_id: The agent run ID.
            result: The agent's output/result.
            tool_calls_count: Number of tool calls made.
            turns_used: Number of turns used.

        Returns:
            Updated AgentRun.
        """
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'success',
                result = ?,
                tool_calls_count = ?,
                turns_used = ?,
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (result, tool_calls_count, turns_used, now, now, run_id),
        )
        return self.get(run_id)

    def fail(
        self,
        run_id: str,
        error: str,
        tool_calls_count: int = 0,
        turns_used: int = 0,
    ) -> AgentRun | None:
        """
        Mark agent run as failed.

        Args:
            run_id: The agent run ID.
            error: Error message.
            tool_calls_count: Number of tool calls made before failure.
            turns_used: Number of turns used before failure.

        Returns:
            Updated AgentRun.
        """
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'error',
                error = ?,
                tool_calls_count = ?,
                turns_used = ?,
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (error, tool_calls_count, turns_used, now, now, run_id),
        )
        return self.get(run_id)

    def timeout(self, run_id: str, turns_used: int = 0) -> AgentRun | None:
        """Mark agent run as timed out."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'timeout',
                error = 'Execution timed out',
                turns_used = ?,
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (turns_used, now, now, run_id),
        )
        return self.get(run_id)

    def cancel(self, run_id: str) -> AgentRun | None:
        """Mark agent run as cancelled."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'cancelled', completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, run_id),
        )
        return self.get(run_id)

    def update_sdk_session_id(self, run_id: str, sdk_session_id: str) -> AgentRun | None:
        """Store the SDK session ID for cross-mode resume.

        Args:
            run_id: The agent run ID.
            sdk_session_id: The Claude CLI session ID captured from ResultMessage.

        Returns:
            Updated AgentRun.
        """
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE agent_runs
            SET sdk_session_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (sdk_session_id, now, run_id),
        )
        return self.get(run_id)

    def get_sdk_session_id_for_session(self, session_id: str) -> str | None:
        """Find SDK session ID for a session that was an agent run.

        Looks up agent_runs where child_session_id matches, returning
        the most recent sdk_session_id.
        """
        row = self.db.fetchone(
            """
            SELECT sdk_session_id FROM agent_runs
            WHERE child_session_id = ? AND sdk_session_id IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (session_id,),
        )
        return row["sdk_session_id"] if row else None

    def update_child_session(self, run_id: str, child_session_id: str) -> AgentRun | None:
        """Update the child session ID for an agent run."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE agent_runs
            SET child_session_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (child_session_id, now, run_id),
        )
        return self.get(run_id)

    def list_by_session(
        self,
        parent_session_id: str,
        status: AgentRunStatus | None = None,
        limit: int = 100,
    ) -> list[AgentRun]:
        """
        List agent runs for a session.

        Args:
            parent_session_id: The parent session ID.
            status: Optional status filter.
            limit: Maximum number of results.

        Returns:
            List of AgentRun objects.
        """
        if status:
            rows = self.db.fetchall(
                """
                SELECT * FROM agent_runs
                WHERE parent_session_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (parent_session_id, status, limit),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM agent_runs
                WHERE parent_session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (parent_session_id, limit),
            )
        return [AgentRun.from_row(row) for row in rows]

    def list_by_status(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> list[AgentRun]:
        """
        List agent runs, optionally filtered by status.

        Args:
            status: Optional status filter.
            limit: Maximum number of results.

        Returns:
            List of AgentRun objects.
        """
        if status:
            rows = self.db.fetchall(
                """
                SELECT * FROM agent_runs
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (status, limit),
            )
        else:
            rows = self.db.fetchall(
                """
                SELECT * FROM agent_runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [AgentRun.from_row(row) for row in rows]

    def list_running(self, limit: int = 100) -> list[AgentRun]:
        """List all currently running agent runs."""
        rows = self.db.fetchall(
            """
            SELECT * FROM agent_runs
            WHERE status = 'running'
            ORDER BY started_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [AgentRun.from_row(row) for row in rows]

    def count_by_session(self, parent_session_id: str) -> dict[str, int]:
        """
        Count agent runs by status for a session.

        Args:
            parent_session_id: The parent session ID.

        Returns:
            Dict mapping status to count.
        """
        rows = self.db.fetchall(
            """
            SELECT status, COUNT(*) as count
            FROM agent_runs
            WHERE parent_session_id = ?
            GROUP BY status
            """,
            (parent_session_id,),
        )
        return {row["status"]: row["count"] for row in rows}

    def delete(self, run_id: str) -> bool:
        """Delete an agent run."""
        cursor = self.db.execute("DELETE FROM agent_runs WHERE id = ?", (run_id,))
        return bool(cursor.rowcount and cursor.rowcount > 0)

    def cleanup_stale_runs(self, timeout_minutes: int = 30) -> int:
        """
        Mark stale running agent runs as timed out.

        Args:
            timeout_minutes: Minutes of inactivity before timeout.

        Returns:
            Number of runs timed out.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'timeout',
                error = 'Stale run timed out',
                completed_at = ?,
                updated_at = ?
            WHERE status = 'running'
            AND datetime(started_at) < datetime('now', 'utc', ? || ' minutes')
            """,
            (now, now, f"-{timeout_minutes}"),
        )
        count = cursor.rowcount or 0
        if count > 0:
            logger.info(f"Timed out {count} stale agent runs (>{timeout_minutes}m)")
        return count

    def cleanup_stale_pending_runs(self, timeout_minutes: int = 60) -> int:
        """
        Mark stale pending agent runs as failed.

        Pending runs that never started within the timeout period are marked as errors.

        Args:
            timeout_minutes: Minutes since creation before marking as failed.

        Returns:
            Number of runs failed.
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'error',
                error = 'Pending run never started',
                completed_at = ?,
                updated_at = ?
            WHERE status = 'pending'
            AND datetime(created_at) < datetime('now', 'utc', ? || ' minutes')
            """,
            (now, now, f"-{timeout_minutes}"),
        )
        count = cursor.rowcount or 0
        if count > 0:
            logger.info(f"Failed {count} stale pending agent runs (>{timeout_minutes}m)")
        return count
