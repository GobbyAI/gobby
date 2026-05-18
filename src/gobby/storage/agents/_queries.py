"""Read and delete operations for agent run storage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from gobby.storage.database import DatabaseProtocol

from ._constants import AgentRunStatus
from ._helpers import _positive_rowcount
from ._models import AgentRun


class _AgentRunQueryHost(Protocol):
    db: DatabaseProtocol

    def _fetch_run_with_live_stats(
        self,
        where_clause: str,
        params: Sequence[object],
    ) -> AgentRun | None: ...

    def _fetch_runs_with_live_stats(
        self,
        where_clause: str = "",
        params: Sequence[object] = (),
        *,
        order_by: str = "",
        limit: int | None = None,
    ) -> list[AgentRun]: ...


class _AgentRunQueryMixin:
    def get(self: _AgentRunQueryHost, run_id: str) -> AgentRun | None:
        """Get agent run by ID."""
        return self._fetch_run_with_live_stats("WHERE ar.id = ?", (run_id,))

    def has_active_run_for_task(self: _AgentRunQueryHost, task_id: str) -> bool:
        """Check if there's already a pending/running agent run for a task."""
        row = self.db.fetchone(
            "SELECT id FROM agent_runs WHERE task_id = ? AND status IN ('pending', 'running')",
            (task_id,),
        )
        return row is not None

    def get_active_run_for_task(self: _AgentRunQueryHost, task_id: str) -> AgentRun | None:
        """Get the active (pending/running) agent run for a task, if any."""
        runs = self._fetch_runs_with_live_stats(
            "WHERE ar.task_id = ? AND ar.status IN ('pending', 'running')",
            (task_id,),
            order_by="ORDER BY ar.created_at DESC",
            limit=1,
        )
        return runs[0] if runs else None

    def list_by_session(
        self: _AgentRunQueryHost,
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
        self: _AgentRunQueryHost,
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

    def list_running(self: _AgentRunQueryHost, limit: int = 100) -> list[AgentRun]:
        """List all currently running agent runs."""
        return self._fetch_runs_with_live_stats(
            "WHERE ar.status = 'running'",
            order_by="ORDER BY ar.started_at ASC",
            limit=limit,
        )

    def list_active(
        self: _AgentRunQueryHost,
        limit: int = 100,
        *,
        task_ids: Sequence[str] | None = None,
    ) -> list[AgentRun]:
        """List all active (running or pending) agent runs."""
        params: list[object] = []
        where_clause = "WHERE ar.status IN ('running', 'pending')"
        if task_ids is not None:
            if not task_ids:
                return []
            placeholders = ", ".join("?" for _ in task_ids)
            where_clause += f" AND ar.task_id IN ({placeholders})"
            params.extend(task_ids)
        return self._fetch_runs_with_live_stats(
            where_clause,
            params,
            order_by="ORDER BY ar.started_at ASC",
            limit=limit,
        )

    def get_by_session(self: _AgentRunQueryHost, session_id: str) -> AgentRun | None:
        """Get active agent run by child session ID."""
        runs = self._fetch_runs_with_live_stats(
            "WHERE ar.child_session_id = ? AND ar.status IN ('running', 'pending')",
            (session_id,),
            order_by="ORDER BY ar.created_at DESC",
            limit=1,
        )
        return runs[0] if runs else None

    def list_by_parent(
        self: _AgentRunQueryHost,
        parent_session_id: str,
        limit: int = 100,
    ) -> list[AgentRun]:
        """List active agent runs spawned by a parent session."""
        return self._fetch_runs_with_live_stats(
            "WHERE ar.parent_session_id = ? AND ar.status IN ('running', 'pending')",
            (parent_session_id,),
            order_by="ORDER BY ar.started_at ASC",
            limit=limit,
        )

    def count_by_session(self: _AgentRunQueryHost, parent_session_id: str) -> dict[str, int]:
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

    def delete(self: _AgentRunQueryHost, run_id: str) -> bool:
        """Delete an agent run."""
        cursor = self.db.execute("DELETE FROM agent_runs WHERE id = ?", (run_id,))
        return bool(_positive_rowcount(cursor))
