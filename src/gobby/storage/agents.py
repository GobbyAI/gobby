"""Storage manager for agent runs."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

AgentRunStatus = Literal["pending", "running", "success", "error", "timeout", "cancelled"]
AgentRunTerminalReason = Literal["user_cancelled", "daemon_restart"]
ACTIVE_AGENT_RUN_STATUSES: tuple[AgentRunStatus, ...] = ("pending", "running")
ACTIVE_AGENT_RUN_STATUS_SQL = ", ".join(f"'{status}'" for status in ACTIVE_AGENT_RUN_STATUSES)
TERMINAL_AGENT_RUN_STATUSES: tuple[AgentRunStatus, ...] = (
    "success",
    "error",
    "timeout",
    "cancelled",
)


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
    claimed_session_id: str | None = None
    workflow_name: str | None = None
    agent_name: str | None = None
    model: str | None = None
    requested_reasoning_effort: str | None = None
    effective_reasoning_effort: str | None = None
    reasoning_required: bool = False
    reasoning_status: str = "not_requested"
    reasoning_message: str | None = None
    result: str | None = None
    error: str | None = None
    tool_calls_count: int = 0
    turns_used: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    sdk_session_id: str | None = None
    continuation_prompt: str | None = None
    task_id: str | None = None
    pid: int | None = None
    tmux_session_name: str | None = None
    worktree_id: str | None = None
    clone_id: str | None = None
    timeout_seconds: float | None = None
    terminal_reason: AgentRunTerminalReason | None = None

    @classmethod
    def from_row(cls, row: Any) -> AgentRun:
        """Create AgentRun from database row."""
        return cls(
            id=row["id"],
            parent_session_id=row["parent_session_id"],
            child_session_id=row["child_session_id"],
            claimed_session_id=(
                row["claimed_session_id"] if "claimed_session_id" in row.keys() else None
            ),
            workflow_name=row["workflow_name"],
            agent_name=row["agent_name"] if "agent_name" in row.keys() else None,
            provider=row["provider"],
            model=row["model"],
            requested_reasoning_effort=(
                row["requested_reasoning_effort"]
                if "requested_reasoning_effort" in row.keys()
                else None
            ),
            effective_reasoning_effort=(
                row["effective_reasoning_effort"]
                if "effective_reasoning_effort" in row.keys()
                else None
            ),
            reasoning_required=bool(row["reasoning_required"])
            if "reasoning_required" in row.keys()
            else False,
            reasoning_status=(
                row["reasoning_status"] if "reasoning_status" in row.keys() else "not_requested"
            ),
            reasoning_message=(
                row["reasoning_message"] if "reasoning_message" in row.keys() else None
            ),
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
            pid=row["pid"] if "pid" in row.keys() else None,
            tmux_session_name=row["tmux_session_name"]
            if "tmux_session_name" in row.keys()
            else None,
            worktree_id=row["worktree_id"] if "worktree_id" in row.keys() else None,
            clone_id=row["clone_id"] if "clone_id" in row.keys() else None,
            timeout_seconds=row["timeout_seconds"] if "timeout_seconds" in row.keys() else None,
            terminal_reason=row["terminal_reason"] if "terminal_reason" in row.keys() else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "run_id": self.id,
            "id": self.id,
            "session_id": self.child_session_id,
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
            "claimed_session_id": self.claimed_session_id,
            "workflow_name": self.workflow_name,
            "agent_name": self.agent_name,
            "provider": self.provider,
            "model": self.model,
            "requested_reasoning_effort": self.requested_reasoning_effort,
            "effective_reasoning_effort": self.effective_reasoning_effort,
            "reasoning_required": self.reasoning_required,
            "reasoning_status": self.reasoning_status,
            "reasoning_message": self.reasoning_message,
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
            "task_id": self.task_id,
            "pid": self.pid,
            "tmux_session_name": self.tmux_session_name,
            "worktree_id": self.worktree_id,
            "clone_id": self.clone_id,
            "timeout_seconds": self.timeout_seconds,
            "continuation_prompt": self.continuation_prompt,
            "terminal_reason": self.terminal_reason,
        }

    def to_brief(self) -> dict[str, Any]:
        """Slim representation for list operations."""
        return {
            "run_id": self.id,
            "session_id": self.child_session_id,
            "parent_session_id": self.parent_session_id,
            "started_at": self.started_at,
            "pid": self.pid,
            "provider": self.provider,
            "task_id": self.task_id,
            "status": self.status,
            "terminal_reason": self.terminal_reason,
            "tool_calls_count": self.tool_calls_count,
            "turns_used": self.turns_used,
        }


class LocalAgentRunManager:
    """Manager for agent run storage operations."""

    def __init__(self, db: DatabaseProtocol):
        """Initialize with database connection."""
        self.db = db

    @staticmethod
    def _select_runs_with_live_stats_sql(
        where_clause: str = "",
        order_by: str = "",
        *,
        limit: bool = False,
    ) -> str:
        """Build an agent-run SELECT that overlays live session stats for active runs."""
        sql = f"""
            SELECT
                ar.id,
                ar.parent_session_id,
                ar.child_session_id,
                ar.claimed_session_id,
                ar.workflow_name,
                ar.agent_name,
                ar.provider,
                ar.model,
                ar.requested_reasoning_effort,
                ar.effective_reasoning_effort,
                ar.reasoning_required,
                ar.reasoning_status,
                ar.reasoning_message,
                ar.status,
                ar.prompt,
                ar.result,
                ar.error,
                CASE
                    WHEN ar.status IN ({ACTIVE_AGENT_RUN_STATUS_SQL}) THEN COALESCE(
                        child_s.tool_call_count,
                        CASE
                            WHEN child_s.id IS NULL THEN parent_s.tool_call_count
                        END,
                        ar.tool_calls_count,
                        0
                    )
                    ELSE COALESCE(ar.tool_calls_count, 0)
                END AS tool_calls_count,
                CASE
                    WHEN ar.status IN ({ACTIVE_AGENT_RUN_STATUS_SQL}) THEN COALESCE(
                        child_s.turn_count,
                        CASE
                            WHEN child_s.id IS NULL THEN parent_s.turn_count
                        END,
                        ar.turns_used,
                        0
                    )
                    ELSE COALESCE(ar.turns_used, 0)
                END AS turns_used,
                ar.started_at,
                ar.completed_at,
                ar.created_at,
                ar.updated_at,
                ar.sdk_session_id,
                ar.continuation_prompt,
                ar.task_id,
                ar.pid,
                ar.tmux_session_name,
                ar.worktree_id,
                ar.clone_id,
                ar.timeout_seconds,
                ar.terminal_reason
            FROM agent_runs ar
            LEFT JOIN sessions child_s ON child_s.id = ar.child_session_id
            LEFT JOIN sessions parent_s ON parent_s.id = ar.parent_session_id
            {where_clause}
            {order_by}
            """
        if limit:
            sql += "\n            LIMIT ?"
        return sql

    def _fetch_run_with_live_stats(
        self,
        where_clause: str,
        params: Sequence[object],
    ) -> AgentRun | None:
        """Fetch one agent run through the live-stat selector."""
        row = self.db.fetchone(
            self._select_runs_with_live_stats_sql(where_clause),
            tuple(params),
        )
        return AgentRun.from_row(row) if row else None

    def _fetch_runs_with_live_stats(
        self,
        where_clause: str = "",
        params: Sequence[object] = (),
        *,
        order_by: str = "",
        limit: int | None = None,
    ) -> list[AgentRun]:
        """Fetch agent runs through the live-stat selector."""
        query_params = tuple(params)
        if limit is not None:
            query_params = (*query_params, limit)

        rows = self.db.fetchall(
            self._select_runs_with_live_stats_sql(
                where_clause,
                order_by,
                limit=limit is not None,
            ),
            query_params,
        )
        return [AgentRun.from_row(row) for row in rows]

    def create(
        self,
        parent_session_id: str,
        provider: str,
        prompt: str,
        workflow_name: str | None = None,
        agent_name: str | None = None,
        model: str | None = None,
        requested_reasoning_effort: str | None = None,
        effective_reasoning_effort: str | None = None,
        reasoning_required: bool = False,
        reasoning_status: str = "not_requested",
        reasoning_message: str | None = None,
        child_session_id: str | None = None,
        claimed_session_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> AgentRun:
        """
        Create a new agent run.

        Args:
            parent_session_id: Session that spawned this agent.
            provider: LLM provider (claude, gemini, etc.)
            prompt: The prompt given to the agent.
            workflow_name: Optional workflow being executed.
            agent_name: Agent definition name used to spawn the run.
            model: Optional model override.
            child_session_id: Optional child session for the agent.
            claimed_session_id: Session that owned the task when the run was created.
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
                id, parent_session_id, child_session_id, claimed_session_id,
                workflow_name, agent_name,
                provider, model,
                requested_reasoning_effort, effective_reasoning_effort,
                reasoning_required, reasoning_status, reasoning_message,
                status, prompt, task_id, timeout_seconds,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                parent_session_id,
                child_session_id,
                claimed_session_id,
                workflow_name,
                agent_name,
                provider,
                model,
                requested_reasoning_effort,
                effective_reasoning_effort,
                int(reasoning_required),
                reasoning_status,
                reasoning_message,
                prompt,
                task_id,
                timeout_seconds,
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
        return self._fetch_run_with_live_stats("WHERE ar.id = ?", (run_id,))

    def has_active_run_for_task(self, task_id: str) -> bool:
        """Check if there's already a pending/running agent run for a task."""
        row = self.db.fetchone(
            "SELECT id FROM agent_runs WHERE task_id = ? AND status IN ('pending', 'running')",
            (task_id,),
        )
        return row is not None

    def get_active_run_for_task(self, task_id: str) -> AgentRun | None:
        """Get the active (pending/running) agent run for a task, if any."""
        runs = self._fetch_runs_with_live_stats(
            "WHERE ar.task_id = ? AND ar.status IN ('pending', 'running')",
            (task_id,),
            order_by="ORDER BY ar.created_at DESC",
            limit=1,
        )
        return runs[0] if runs else None

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

    def _expire_sessions_for_run_ids(self, run_ids: Sequence[str]) -> int:
        """Expire active child sessions associated with terminal agent runs."""
        filtered_run_ids = [run_id for run_id in run_ids if run_id]
        if not filtered_run_ids:
            return 0

        placeholders = ", ".join("?" for _ in filtered_run_ids)
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            f"""
            UPDATE sessions
            SET status = 'expired',
                updated_at = ?
            WHERE status IN ('active', 'paused')
            AND (
                agent_run_id IN ({placeholders})
                OR id IN (
                    SELECT child_session_id
                    FROM agent_runs
                    WHERE id IN ({placeholders})
                    AND child_session_id IS NOT NULL
                )
            )
            """,
            (now, *filtered_run_ids, *filtered_run_ids),
        )
        return cursor.rowcount or 0

    def expire_sessions_for_terminal_runs(self) -> int:
        """Expire active/paused child sessions whose agent run is already terminal."""
        status_placeholders = ", ".join("?" for _ in TERMINAL_AGENT_RUN_STATUSES)
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            f"""
            UPDATE sessions
            SET status = 'expired',
                updated_at = ?
            WHERE status IN ('active', 'paused')
            AND (
                agent_run_id IN (
                    SELECT id
                    FROM agent_runs
                    WHERE status IN ({status_placeholders})
                )
                OR id IN (
                    SELECT child_session_id
                    FROM agent_runs
                    WHERE status IN ({status_placeholders})
                    AND child_session_id IS NOT NULL
                )
            )
            """,
            (now, *TERMINAL_AGENT_RUN_STATUSES, *TERMINAL_AGENT_RUN_STATUSES),
        )
        return cursor.rowcount or 0

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
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'success',
                result = ?,
                terminal_reason = NULL,
                tool_calls_count = ?,
                turns_used = ?,
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
              AND status IN ('pending', 'running')
            """,
            (result, tool_calls_count, turns_used, now, now, run_id),
        )
        if not (cursor.rowcount or 0):
            return None
        self._expire_sessions_for_run_ids([run_id])
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
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'error',
                error = ?,
                terminal_reason = NULL,
                tool_calls_count = ?,
                turns_used = ?,
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
              AND status IN ('pending', 'running')
            """,
            (error, tool_calls_count, turns_used, now, now, run_id),
        )
        if not (cursor.rowcount or 0):
            return None
        self._expire_sessions_for_run_ids([run_id])
        return self.get(run_id)

    def timeout(
        self,
        run_id: str,
        turns_used: int = 0,
        error: str = "Execution timed out",
        tool_calls_count: int = 0,
    ) -> AgentRun | None:
        """Mark agent run as timed out."""
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'timeout',
                error = ?,
                terminal_reason = NULL,
                tool_calls_count = ?,
                turns_used = ?,
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
              AND status IN ('pending', 'running')
            """,
            (error, tool_calls_count, turns_used, now, now, run_id),
        )
        if not (cursor.rowcount or 0):
            return None
        self._expire_sessions_for_run_ids([run_id])
        return self.get(run_id)

    def cancel(
        self,
        run_id: str,
        *,
        terminal_reason: AgentRunTerminalReason | None = None,
    ) -> AgentRun | None:
        """Mark agent run as cancelled."""
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'cancelled',
                terminal_reason = ?,
                completed_at = ?,
                updated_at = ?
            WHERE id = ?
              AND status IN ('pending', 'running')
            """,
            (terminal_reason, now, now, run_id),
        )
        if not (cursor.rowcount or 0):
            return None
        self._expire_sessions_for_run_ids([run_id])
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

    def update_runtime(
        self,
        run_id: str,
        *,
        pid: int | None = None,
        tmux_session_name: str | None = None,
        worktree_id: str | None = None,
        clone_id: str | None = None,
    ) -> None:
        """Persist runtime state for an agent run (pid, tmux session, mode, isolation).

        Only updates fields that are provided (non-None).
        """
        updates: list[str] = []
        params: list[Any] = []

        if pid is not None:
            updates.append("pid = ?")
            params.append(pid)
        if tmux_session_name is not None:
            updates.append("tmux_session_name = ?")
            params.append(tmux_session_name)
        if worktree_id is not None:
            updates.append("worktree_id = ?")
            params.append(worktree_id)
        if clone_id is not None:
            updates.append("clone_id = ?")
            params.append(clone_id)

        if not updates:
            return

        now = datetime.now(UTC).isoformat()
        updates.append("updated_at = ?")
        params.append(now)
        params.append(run_id)

        self.db.execute(
            f"UPDATE agent_runs SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )

    def list_pending_with_pid(self, limit: int = 100) -> list[AgentRun]:
        """List pending agent runs that have a PID (spawned but not yet marked running)."""
        return self._fetch_runs_with_live_stats(
            "WHERE ar.status = 'pending' AND ar.pid IS NOT NULL",
            order_by="ORDER BY ar.created_at ASC",
            limit=limit,
        )

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
        conditions = ["ar.parent_session_id = ?"]
        params: list[object] = [parent_session_id]
        if status:
            conditions.append("ar.status = ?")
            params.append(status)

        return self._fetch_runs_with_live_stats(
            f"WHERE {' AND '.join(conditions)}",
            params,
            order_by="ORDER BY ar.created_at DESC",
            limit=limit,
        )

    def list_by_status(
        self,
        status: str | None = None,
        limit: int = 100,
        project_id: str | None = None,
    ) -> list[AgentRun]:
        """
        List agent runs, optionally filtered by status and/or project.

        Args:
            status: Optional status filter.
            limit: Maximum number of results.
            project_id: Optional project ID filter (joins through sessions).

        Returns:
            List of AgentRun objects.
        """
        conditions: list[str] = []
        params: list[object] = []

        if status:
            conditions.append("ar.status = ?")
            params.append(status)

        if project_id:
            conditions.append("parent_s.project_id = ?")
            params.append(project_id)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        return self._fetch_runs_with_live_stats(
            where_clause,
            params,
            order_by="ORDER BY ar.created_at DESC",
            limit=limit,
        )

    def list_running(self, limit: int = 100) -> list[AgentRun]:
        """List all currently running agent runs."""
        return self._fetch_runs_with_live_stats(
            "WHERE ar.status = 'running'",
            order_by="ORDER BY ar.started_at ASC",
            limit=limit,
        )

    def list_active(self, limit: int = 100) -> list[AgentRun]:
        """List all active (running or pending) agent runs."""
        return self._fetch_runs_with_live_stats(
            "WHERE ar.status IN ('running', 'pending')",
            order_by="ORDER BY ar.started_at ASC",
            limit=limit,
        )

    def get_by_session(self, session_id: str) -> AgentRun | None:
        """Get active agent run by child session ID."""
        runs = self._fetch_runs_with_live_stats(
            "WHERE ar.child_session_id = ? AND ar.status IN ('running', 'pending')",
            (session_id,),
            order_by="ORDER BY ar.created_at DESC",
            limit=1,
        )
        return runs[0] if runs else None

    def list_by_parent(self, parent_session_id: str, limit: int = 100) -> list[AgentRun]:
        """List active agent runs spawned by a parent session."""
        return self._fetch_runs_with_live_stats(
            "WHERE ar.parent_session_id = ? AND ar.status IN ('running', 'pending')",
            (parent_session_id,),
            order_by="ORDER BY ar.started_at ASC",
            limit=limit,
        )

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

    def cleanup_stale_runs(self, default_timeout_minutes: int = 30) -> int:
        """Mark stale running agent runs as timed out and expire their sessions.

        Uses per-agent timeout_seconds when set, falls back to default_timeout_minutes.

        Args:
            default_timeout_minutes: Fallback timeout for runs without timeout_seconds.

        Returns:
            Number of runs timed out.
        """
        stale_runs = self.db.fetchall(
            """
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
                AND (julianday('now') - julianday(last_activity_at)) * 86400 > timeout_seconds
            )
            OR (
                timeout_seconds IS NULL
                AND datetime(last_activity_at) < datetime('now', 'utc', ? || ' minutes')
            )
            """,
            (f"-{default_timeout_minutes}",),
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
            logger.info(
                "Timed out %s stale agent runs (%s explicit, %s default)",
                timed_out,
                explicit_count,
                default_count,
            )

        return timed_out

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
