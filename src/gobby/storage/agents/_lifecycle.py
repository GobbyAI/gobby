"""Lifecycle operations for agent run storage."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

from gobby.storage.database import DatabaseProtocol

from ._constants import TERMINAL_AGENT_RUN_STATUSES, AgentRunTerminalReason, get_logger
from ._helpers import _positive_rowcount, utc_now_iso
from ._models import AgentRun


class _AgentRunLifecycleHost(Protocol):
    db: DatabaseProtocol

    def get(self, run_id: str) -> AgentRun | None: ...

    def _expire_sessions_for_run_ids(self, run_ids: Sequence[str]) -> int: ...


class _AgentRunLifecycleMixin:
    def create(
        self: _AgentRunLifecycleHost,
        parent_session_id: str,
        provider: str,
        prompt: str,
        workflow_name: str | None = None,
        agent_name: str | None = None,
        model: str | None = None,
        is_local: bool = False,
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
        now = utc_now_iso()

        self.db.execute(
            """
            INSERT INTO agent_runs (
                id, parent_session_id, child_session_id, claimed_session_id,
                workflow_name, agent_name,
                provider, model, is_local,
                requested_reasoning_effort, effective_reasoning_effort,
                reasoning_required, reasoning_status, reasoning_message,
                status, prompt, task_id, timeout_seconds,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
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
                int(is_local),
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

        get_logger().debug(
            "Created agent run %s for session %s",
            run_id,
            parent_session_id,
        )
        agent_run = self.get(run_id)
        if agent_run is None:
            raise RuntimeError(f"Failed to retrieve newly created agent run: {run_id}")
        return agent_run

    def start(self: _AgentRunLifecycleHost, run_id: str) -> AgentRun | None:
        """Mark agent run as started."""
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'running', started_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, run_id),
        )
        return self.get(run_id)

    def _expire_sessions_for_run_ids(
        self: _AgentRunLifecycleHost,
        run_ids: Sequence[str],
    ) -> int:
        """Expire active child sessions associated with terminal agent runs."""
        filtered_run_ids = [run_id for run_id in run_ids if run_id]
        if not filtered_run_ids:
            return 0

        placeholders = ", ".join("?" for _ in filtered_run_ids)
        now = utc_now_iso()
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
        return _positive_rowcount(cursor)

    def expire_sessions_for_terminal_runs(self: _AgentRunLifecycleHost) -> int:
        """Expire active/paused child sessions whose agent run is already terminal."""
        status_placeholders = ", ".join("?" for _ in TERMINAL_AGENT_RUN_STATUSES)
        now = utc_now_iso()
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
        return _positive_rowcount(cursor)

    def complete(
        self: _AgentRunLifecycleHost,
        run_id: str,
        result: str | None = None,
        tool_calls_count: int = 0,
        turns_used: int = 0,
    ) -> AgentRun | None:
        """
        Mark agent run as completed successfully.

        Args:
            run_id: The agent run ID.
            result: Optional agent output/result override. When omitted, the
                current stored result is preserved.
            tool_calls_count: Number of tool calls made.
            turns_used: Number of turns used.

        Returns:
            Updated AgentRun.
        """
        now = utc_now_iso()
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'success',
                result = COALESCE(?, result),
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
        if not _positive_rowcount(cursor):
            return None
        self._expire_sessions_for_run_ids([run_id])
        return self.get(run_id)

    def fail(
        self: _AgentRunLifecycleHost,
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
        now = utc_now_iso()
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
        if not _positive_rowcount(cursor):
            return None
        self._expire_sessions_for_run_ids([run_id])
        return self.get(run_id)

    def timeout(
        self: _AgentRunLifecycleHost,
        run_id: str,
        turns_used: int = 0,
        error: str = "Execution timed out",
        tool_calls_count: int = 0,
    ) -> AgentRun | None:
        """Mark agent run as timed out."""
        now = utc_now_iso()
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
        if not _positive_rowcount(cursor):
            return None
        self._expire_sessions_for_run_ids([run_id])
        return self.get(run_id)

    def cancel(
        self: _AgentRunLifecycleHost,
        run_id: str,
        *,
        terminal_reason: AgentRunTerminalReason | None = None,
    ) -> AgentRun | None:
        """Mark agent run as cancelled."""
        now = utc_now_iso()
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
        if not _positive_rowcount(cursor):
            return None
        self._expire_sessions_for_run_ids([run_id])
        return self.get(run_id)
